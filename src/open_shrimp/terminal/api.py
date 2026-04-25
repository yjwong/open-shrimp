"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing log sources (background task output,
container build logs, etc.), a REST endpoint for reading their content,
and a WebSocket PTY endpoint for interactive ``claude auth login``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import struct
import termios
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.claude_binary import find_claude_binary
from open_shrimp.config import Config
from open_shrimp.review.auth import AuthError, authenticate, validate_token_param
from open_shrimp.terminal.jsonl_render import render_jsonl_content, render_jsonl_lines
from open_shrimp.terminal.log_source import LogSource, resolve

logger = logging.getLogger(__name__)


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await authenticate(
        authorization, config.telegram.token, config.allowed_users
    )


def _resolve_source(request: Request) -> LogSource | None:
    """Extract ``type``, ``id``, and optional ``task_type`` from query
    params and resolve to a ``LogSource``."""
    source_type = request.query_params.get("type", "")
    source_id = request.query_params.get("id", "")
    task_type = request.query_params.get("task_type")
    if not source_type or not source_id:
        return None
    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    return resolve(
        source_type, source_id, task_type=task_type,
        sandbox_managers=sandbox_managers,
    )


async def tail_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """GET /api/terminal/tail — SSE stream tailing a log source.

    Query params:
        type: Log source type (``"task"`` or ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        offset: Byte offset to start reading from (default 0).
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    source = _resolve_source(request)
    if source is None:
        return JSONResponse(
            {"error": "type and id are required"}, status_code=400
        )

    offset = int(request.query_params.get("offset", "0"))
    if offset < 0:
        offset = 0

    is_agent = source.render == "jsonl"

    async def event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events as the file grows.

        Uses ``watchfiles.awatch()`` to react to file changes via native
        OS mechanisms (FSEvents on macOS, inotify on Linux) instead of
        polling with ``stat()``.
        """
        from watchfiles import awatch

        pos = offset
        line_buffer = ""  # carries incomplete JSONL lines (agent only)
        DRAIN_TIMEOUT = 3.0  # seconds to drain after source becomes inactive
        GLOBAL_TIMEOUT = 300.0  # hard cap for orphaned connections

        def _flush_and_done(completed: bool) -> str:
            """Flush remaining agent buffer and return done SSE events."""
            parts = ""
            if is_agent and line_buffer.strip():
                rendered, _ = render_jsonl_lines(line_buffer + "\n")
                if rendered:
                    payload = json.dumps({
                        "text": rendered,
                        "offset": pos,
                    })
                    parts += f"data: {payload}\n\n"
            done_data = json.dumps({"completed": completed})
            parts += f"event: done\ndata: {done_data}\n\n"
            return parts

        def _read_new_data() -> tuple[str | None, int]:
            """Read any new bytes past *pos*. Returns (chunk, new_size)."""
            try:
                size = source.path.stat().st_size
            except FileNotFoundError:
                return None, -1
            if size <= pos:
                return "", size
            chunk = _read_chunk(source.path, pos, size)
            return chunk, size

        def _yield_chunk(chunk: str, file_size: int) -> str | None:
            """Render a chunk and return an SSE payload string (or None)."""
            nonlocal line_buffer, pos
            if not chunk:
                return None
            if is_agent:
                text_to_render = line_buffer + chunk
                rendered, line_buffer = render_jsonl_lines(text_to_render)
                if rendered:
                    pos = file_size
                    payload = json.dumps({
                        "text": rendered, "offset": file_size,
                    })
                    return f"data: {payload}\n\n"
                pos = file_size
                return None
            else:
                pos = file_size
                payload = json.dumps({
                    "text": chunk, "offset": file_size,
                })
                return f"data: {payload}\n\n"

        # -- Coordinating event: signals all watchers to stop --
        stop_event = asyncio.Event()

        async def watch_disconnect() -> None:
            """Detect SSE client disconnect."""
            while not stop_event.is_set():
                if await request.is_disconnected():
                    stop_event.set()
                    return
                await asyncio.sleep(1)

        async def check_completion() -> None:
            """Detect when the source task finishes."""
            await asyncio.sleep(2)  # let the task start producing output
            while not stop_event.is_set():
                if not source.is_active():
                    # Drain period: wait for final writes to land.
                    await asyncio.sleep(DRAIN_TIMEOUT)
                    if not source.is_active():
                        stop_event.set()
                        return
                await asyncio.sleep(2)

        disconnect_task = asyncio.create_task(watch_disconnect())
        completion_task = asyncio.create_task(check_completion())

        try:
            async with asyncio.timeout(GLOBAL_TIMEOUT):
                parent = source.path.parent

                # Wait for the parent directory to appear (rare race).
                if not parent.exists():
                    for _ in range(20):
                        if parent.exists() or stop_event.is_set():
                            break
                        await asyncio.sleep(0.5)
                    if not parent.exists():
                        yield _flush_and_done(completed=False)
                        return

                # Wait for the file itself to appear.
                if not source.path.exists():
                    async for changes in awatch(
                        parent, stop_event=stop_event
                    ):
                        if source.path.exists():
                            break
                    if stop_event.is_set() and not source.path.exists():
                        yield _flush_and_done(
                            completed=not source.is_active()
                        )
                        return

                # For symlinks (e.g. agent task .output -> .jsonl),
                # resolve to the real file so we watch the correct
                # directory where writes actually happen.
                watch_path = source.path.resolve()
                watch_parent = watch_path.parent

                # Catch-up: read any data already present.
                chunk, file_size = await asyncio.to_thread(_read_new_data)
                if file_size == -1:
                    yield _flush_and_done(completed=True)
                    return
                if chunk:
                    sse = _yield_chunk(chunk, file_size)
                    if sse:
                        yield sse

                # Main watch loop — react to file changes via OS events.
                async for changes in awatch(
                    watch_parent, stop_event=stop_event
                ):
                    if stop_event.is_set():
                        break

                    # Only care about changes to our target file.
                    if not any(
                        Path(p) == watch_path for _, p in changes
                    ):
                        continue

                    chunk, file_size = await asyncio.to_thread(
                        _read_new_data
                    )
                    if file_size == -1:
                        # File deleted.
                        break
                    if chunk:
                        sse = _yield_chunk(chunk, file_size)
                        if sse:
                            yield sse

                # Final read: pick up any bytes written after the last
                # change event but before the stop_event fired.
                chunk, file_size = await asyncio.to_thread(_read_new_data)
                if chunk and file_size > 0:
                    sse = _yield_chunk(chunk, file_size)
                    if sse:
                        yield sse

                yield _flush_and_done(
                    completed=not source.is_active()
                )

        except TimeoutError:
            yield _flush_and_done(completed=False)

        finally:
            stop_event.set()
            disconnect_task.cancel()
            completion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnect_task
            with contextlib.suppress(asyncio.CancelledError):
                await completion_task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _read_chunk(path: Path, start: int, end: int) -> str:
    """Read a file chunk from start to end byte positions."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return data.decode("utf-8", errors="replace")


async def read_endpoint(request: Request) -> JSONResponse:
    """GET /api/terminal/read — read the full content of a log source.

    Query params:
        type: Log source type (``"task"`` or ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    source = _resolve_source(request)
    if source is None:
        return JSONResponse(
            {"error": "type and id are required"}, status_code=400
        )

    is_agent = source.render == "jsonl"

    try:
        content = await asyncio.to_thread(source.path.read_text, "utf-8", "replace")
        size = source.path.stat().st_size
    except FileNotFoundError:
        return JSONResponse(
            {"error": "Log source not found"}, status_code=404
        )

    if is_agent:
        content = render_jsonl_content(content)

    return JSONResponse({
        "id": request.query_params.get("id", ""),
        "content": content,
        "size": size,
    })


# ── Login PTY WebSocket endpoint ──
#
# The PTY process is decoupled from the WebSocket lifetime so that the
# user can switch to a browser to complete OAuth and come back without
# the session being killed.  A single ``_LoginSession`` lives at module
# level; WebSocket clients attach/detach freely.
#
# Runs ``claude`` in TUI mode and sends ``/login\n`` to trigger the
# interactive OAuth flow.  The TUI has a built-in paste prompt that
# accepts ``code#state`` via stdin — no localhost callback proxy needed.


class _LoginSession:
    """Background PTY session for ``claude`` TUI login."""

    # Marker printed by ConsoleOAuthFlow when OAuth completes successfully
    # ("Login successful. Press Enter to continue…"). ``/login`` is a slash
    # command that only dismisses its Dialog on Enter — the REPL then sits
    # at the prompt indefinitely. When we see this, we drive the REPL to
    # exit so the subprocess actually terminates.
    _SUCCESS_MARKER = "Login successful"

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        master_fd: int,
    ) -> None:
        self.proc = proc
        self.master_fd = master_fd
        # Circular buffer of terminal output for replay on reconnect.
        self._output_chunks: list[str] = []
        self._output_bytes = 0
        self._MAX_BUFFER = 64 * 1024  # 64 KiB
        self._websocket: WebSocket | None = None
        self._reader_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None
        self._done = asyncio.Event()
        # Small sliding window so the marker is detected even when it
        # straddles two os.read() boundaries.
        self._scan_tail = ""

    def start_background(self) -> None:
        self._reader_task = asyncio.create_task(self._pty_reader())

    # ── Attach / detach WebSocket ──

    async def attach(self, websocket: WebSocket) -> None:
        """Send buffered output, then start forwarding."""
        self._websocket = websocket
        for chunk in self._output_chunks:
            await websocket.send_text(chunk)

    def detach(self) -> None:
        self._websocket = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def wait(self) -> None:
        await self._done.wait()

    # ── Background tasks ──

    async def _pty_reader(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(
                    None, os.read, self.master_fd, 4096
                )
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                # Buffer for replay.
                self._output_chunks.append(text)
                self._output_bytes += len(text)
                while self._output_bytes > self._MAX_BUFFER:
                    removed = self._output_chunks.pop(0)
                    self._output_bytes -= len(removed)
                # Forward to attached WebSocket.
                ws = self._websocket
                if ws is not None:
                    try:
                        await ws.send_text(text)
                    except Exception:
                        self._websocket = None
                # Watch for the success marker and schedule a graceful
                # REPL exit the first time we see it. Scan a small sliding
                # window so a marker split across two reads still matches.
                if self._exit_task is None:
                    combined = self._scan_tail + text
                    if self._SUCCESS_MARKER in combined:
                        self._exit_task = asyncio.create_task(
                            self._graceful_exit()
                        )
                    tail_len = len(self._SUCCESS_MARKER) - 1
                    self._scan_tail = combined[-tail_len:]
        except OSError:
            pass
        finally:
            self._done.set()

    async def _graceful_exit(self) -> None:
        """Drive the REPL to exit after ``/login`` completes.

        The ``/login`` slash command's ``onDone`` only dismisses the Login
        Dialog — it does not terminate the process. We send:

        1. ``\\r`` to fire the "Press Enter to continue…" confirmation,
           which dismisses the dialog and lets the post-login
           fire-and-forget refreshes (``enrollTrustedDevice``,
           ``refreshPolicyLimits``, etc.) start.
        2. A short delay so those in-flight network calls settle.
        3. ``/exit\\r`` which routes through ``gracefulShutdown`` in the
           Claude Code REPL and terminates the subprocess cleanly.
        """
        try:
            os.write(self.master_fd, b"\r")
            # Give the post-login refreshes in commands/login/login.tsx
            # (enrollTrustedDevice, refreshRemoteManagedSettings, etc.)
            # a moment to complete before we tear the REPL down.
            await asyncio.sleep(2.0)
            os.write(self.master_fd, b"/exit\r")
        except OSError:
            pass

    # ── Cleanup ──

    async def destroy(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._exit_task:
            self._exit_task.cancel()
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                async with asyncio.timeout(3):
                    await self.proc.wait()
            except TimeoutError:
                logger.warning(
                    "claude /login (pid=%d) did not exit on SIGTERM, killing",
                    self.proc.pid or 0,
                )
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await self.proc.wait()
        logger.info("Login session destroyed: pid=%d", self.proc.pid or 0)


# The single active login session (if any).
_login_session: _LoginSession | None = None


async def shutdown_login_session() -> None:
    """Destroy any live ``claude /login`` PTY session.

    Called from the bot shutdown path so the long-lived login subprocess
    doesn't outlive the openshrimp service.  Without this, a stale
    ``claude /login`` child sits in the systemd cgroup waiting to be
    reaped by SIGTERM during the next restart, slowing things down.
    """
    global _login_session
    if _login_session is None:
        return
    try:
        await _login_session.destroy()
    except Exception:
        logger.warning("Error destroying login session", exc_info=True)
    _login_session = None


async def login_ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket PTY: spawn or attach to ``claude`` TUI for /login.

    Runs ``claude`` in interactive TUI mode and sends ``/login`` to
    trigger the OAuth flow.  The TUI's built-in paste prompt accepts
    ``code#state`` via stdin — the user copies the code from the
    Anthropic callback page and pastes it in the mini app.

    The PTY process lives independently of the WebSocket so the user
    can switch to the browser and come back without losing the session.

    Query params:
        token: Telegram initData or HMAC token for authentication.
    """
    global _login_session

    config: Config = websocket.app.state.config
    token = websocket.query_params.get("token", "")

    try:
        await validate_token_param(
            token, config.telegram.token, config.allowed_users
        )
    except AuthError:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    # If there's an existing live session, reattach to it.
    if _login_session is not None and _login_session.alive:
        logger.info("Login WS: reattaching to existing session")
        await _login_session.attach(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                _handle_ws_input(raw, _login_session)
        except WebSocketDisconnect:
            _login_session.detach()
            logger.info("Login WS: client detached (session stays alive)")
        except Exception:
            _login_session.detach()
        return

    # Clean up any dead session.
    if _login_session is not None:
        await _login_session.destroy()
        _login_session = None

    # ── Start a new session ──

    try:
        claude_bin = find_claude_binary()
    except RuntimeError as e:
        await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
        await websocket.close()
        return

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 80, 0, 0),
    )

    # BROWSER=echo: openBrowser() "succeeds" without opening anything,
    # and the TUI falls back to showing the paste prompt after 3s.
    # TERM/COLORTERM: enable 256-color and truecolor output in the TUI.
    env = {
        **os.environ,
        "BROWSER": "echo",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }

    proc = await asyncio.create_subprocess_exec(
        claude_bin, "/login",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    logger.info("Login PTY started: pid=%d", proc.pid or 0)

    session = _LoginSession(proc, master_fd)
    _login_session = session
    session.start_background()
    await session.attach(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            _handle_ws_input(raw, session)
    except WebSocketDisconnect:
        session.detach()
        logger.info("Login WS: client detached (session stays alive)")
    except Exception:
        logger.exception("Login WS error")
        session.detach()

    # Don't destroy the session here — it stays alive for reconnection.
    # It will be cleaned up when the process exits and the next connect
    # finds a dead session, or on a fresh /login.


def _handle_ws_input(raw: str, session: _LoginSession) -> None:
    """Parse a WebSocket message and write to the PTY or resize."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        os.write(session.master_fd, raw.encode("utf-8"))
        return

    msg_type = msg.get("type")
    if msg_type == "stdin":
        os.write(session.master_fd, msg["data"].encode("utf-8"))
    elif msg_type == "resize":
        cols = msg.get("cols", 80)
        rows = msg.get("rows", 24)
        fcntl.ioctl(
            session.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )


def create_terminal_routes() -> list[Route | Mount]:
    """Create the routes for the terminal API and Mini App frontend.

    Returns a list of routes to be added to the main Starlette app.
    """
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "web"
        / "terminal-app"
        / "dist"
    )
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount] = [
        Route("/api/terminal/tail", tail_endpoint, methods=["GET"]),
        Route("/api/terminal/read", read_endpoint, methods=["GET"]),
        WebSocketRoute("/ws/terminal/login", login_ws_endpoint),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount(
                "/terminal",
                app=StaticFiles(directory=str(_dist_dir), html=True),
                name="terminal-app",
            )
        )
        logger.info("Serving terminal Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "Terminal Mini App dist directory not found at %s — "
            "run 'npm run build' in web/terminal-app/ to build the frontend",
            _dist_dir,
        )

    return routes
