"""SSE event bus with per-session demultiplexing."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from open_shrimp.backend.errors import CLIConnectionError
from open_shrimp.backend.opencode.process import OpenCodeEndpoint, OpenCodeServer

logger = logging.getLogger("opencode.sse")


_DEFAULT_QUEUE_SIZE = 1024
# Generous: on a fresh sandbox boot the HTTP server is already up (readiness is
# checked separately), but the /event stream's first `server.connected` can lag
# several seconds while the background reader retries. 5s raced and failed.
_CONNECT_TIMEOUT = 30.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_HEALTH_INTERVAL = 1.0

EVT_SERVER_CONNECTED = "server.connected"


class EventQueueClosed(Exception):
    """Raised when a local subscriber queue is closed intentionally."""


class EventQueue:
    """Per-session bounded queue. Drops oldest on overflow."""

    def __init__(self, session_id: str, maxsize: int = _DEFAULT_QUEUE_SIZE) -> None:
        self.session_id = session_id
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._overflow_logged = False
        self._closed = False
        self._error: CLIConnectionError | None = None

    def put_nowait(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if not self._overflow_logged:
                logger.warning(
                    "event queue for session %s overflowed (maxsize=%d); "
                    "dropping oldest events",
                    self.session_id,
                    self._maxsize,
                )
                self._overflow_logged = True
            try:
                self._q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def get(self) -> dict[str, Any]:
        evt = await self._q.get()
        if evt is None:
            self._q.put_nowait(None)
            if self._error is not None:
                raise self._error
            raise EventQueueClosed
        return evt

    def close(self, error: CLIConnectionError | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._error = error
        while not self._q.empty():
            self._q.get_nowait()
        self._q.put_nowait(None)


class EventBus:
    """One long-lived `/event` connection, demultiplexed by sessionID."""

    def __init__(
        self,
        server: OpenCodeServer | OpenCodeEndpoint,
        *,
        directory: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._server = server
        self._owner = getattr(server, "owner", None)
        self._served_proc = getattr(self._owner, "_served_proc", None)
        self._terminal_error: CLIConnectionError | None = None
        self._directory = directory
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=server.base_url,
            timeout=None,
            headers={"Authorization": server.auth_header},
        )
        self._queue_size = queue_size
        self._subscribers: dict[str, EventQueue] = {}
        self._broadcast = EventQueue("__broadcast__", maxsize=queue_size)
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def is_alive(self) -> bool:
        """Unknown process ownership is healthy until the bus itself stops."""
        if self._stop.is_set() or (
            self._reader_task is not None and self._reader_task.done()
        ):
            return False
        if isinstance(self._server, OpenCodeServer):
            return self._server.proc.returncode is None
        if self._served_proc is not None:
            return (
                getattr(self._owner, "_served_proc", None) is self._served_proc
                and self._served_proc.poll() is None
            )
        return True

    async def start(self) -> None:
        async with self._lock:
            if self._stop.is_set():
                raise self._terminal_error or CLIConnectionError("opencode SSE bus is stopped")
            if self._reader_task is None:
                self._reader_task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_CONNECT_TIMEOUT)
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._stop.is_set():
                raise CLIConnectionError("opencode SSE reader stopped before connecting")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise CLIConnectionError(
                f"opencode serve did not emit server.connected within {_CONNECT_TIMEOUT}s"
            ) from exc

    async def stop(self, error: CLIConnectionError | None = None) -> None:
        if error is not None:
            self._terminal_error = error
        self._stop.set()
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            if not self._reader_task.done() and not self._reader_task.cancelling():
                self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("SSE reader task raised on stop: %s", exc)
            self._reader_task = None
        await self._cleanup()

    async def _cleanup(self) -> None:
        for q in list(self._subscribers.values()):
            q.close(self._terminal_error)
        self._subscribers.clear()
        self._broadcast.close(self._terminal_error)
        if self._owns_client and not self._http.is_closed:
            await self._http.aclose()

    def subscribe(self, session_id: str) -> EventQueue:
        q = self._subscribers.get(session_id)
        if q is None:
            q = EventQueue(session_id, maxsize=self._queue_size)
            if self._stop.is_set():
                q.close(self._terminal_error)
            else:
                self._subscribers[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        q = self._subscribers.pop(session_id, None)
        if q is not None:
            q.close()

    async def _run(self) -> None:
        reader = asyncio.current_task()

        async def watch_endpoint() -> None:
            while not self._stop.is_set():
                if not self.is_alive():
                    self._terminal_error = CLIConnectionError(
                        "opencode serve exited or its endpoint was replaced"
                    )
                    self._stop.set()
                    if reader is not None:
                        reader.cancel()
                    return
                await asyncio.sleep(_HEALTH_INTERVAL)

        watcher = asyncio.create_task(watch_endpoint())
        try:
            await self._reconnect()
        except CLIConnectionError as exc:
            self._terminal_error = exc
        finally:
            self._stop.set()
            if self._terminal_error is not None:
                logger.warning(
                    "SSE endpoint lost url=%s directory=%r subscribers=%d",
                    self._server.base_url, self._directory, len(self._subscribers),
                )
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            try:
                await self._cleanup()
            finally:
                self._ready.set()

    async def _reconnect(self) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            if not self.is_alive():
                raise CLIConnectionError("opencode serve exited or its endpoint was replaced")
            try:
                await self._read_stream()
                if self._stop.is_set():
                    return
                logger.warning(
                    "SSE stream ended url=%s directory=%r subscribers=%d; reconnecting in %.1fs",
                    self._server.base_url, self._directory, len(self._subscribers), backoff,
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning(
                    "SSE stream error (%s) url=%s directory=%r subscribers=%d; reconnecting in %.1fs",
                    type(exc).__name__, self._server.base_url, self._directory,
                    len(self._subscribers), backoff,
                )
            # Jittered sleep: avoid thundering-herd if many clients reconnect together.
            sleep_for = backoff * random.uniform(0.5, 1.5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _read_stream(self) -> None:
        params = {"directory": self._directory} if self._directory else None
        async with self._http.stream("GET", "/event", params=params) as r:
            r.raise_for_status()
            async for event in _sse_events(r):
                if self._stop.is_set():
                    return
                self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == EVT_SERVER_CONNECTED and not self._ready.is_set():
            self._ready.set()
        props = event.get("properties") or {}
        sid = props.get("sessionID") if isinstance(props, dict) else None
        target = self._subscribers.get(sid) if sid else None
        if target is not None:
            target.put_nowait(event)
        else:
            self._broadcast.put_nowait(event)


async def _sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    buf = ""
    async for chunk in response.aiter_text():
        buf += chunk
        while "\n\n" in buf:
            raw_event, buf = buf.split("\n\n", 1)
            data_lines = [
                ln[5:].lstrip()
                for ln in raw_event.splitlines()
                if ln.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                yield json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                logger.debug("ignoring un-parseable SSE event: %r", raw_event[:200])
                continue


__all__ = ["EventBus", "EventQueue", "EventQueueClosed"]
