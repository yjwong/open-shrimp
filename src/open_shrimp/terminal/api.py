"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing log sources (background task output,
container build logs, etc.) and a REST endpoint for reading their content.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_shrimp.config import Config
from open_shrimp.review.auth import AuthError, authenticate
from open_shrimp.terminal.jsonl_render import (
    render_jsonl_content,
    render_jsonl_lines,
    render_openshrimp_agent_content,
    render_openshrimp_agent_lines,
)
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

    is_rendered_jsonl = source.render in {"jsonl", "openshrimp-agent-jsonl"}

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
            if is_rendered_jsonl and line_buffer.strip():
                rendered, _ = _render_log_lines(source.render, line_buffer + "\n")
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
            if is_rendered_jsonl:
                text_to_render = line_buffer + chunk
                rendered, line_buffer = _render_log_lines(
                    source.render, text_to_render,
                )
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

    is_rendered_jsonl = source.render in {"jsonl", "openshrimp-agent-jsonl"}

    try:
        content = await asyncio.to_thread(source.path.read_text, "utf-8", "replace")
        size = source.path.stat().st_size
    except FileNotFoundError:
        return JSONResponse(
            {"error": "Log source not found"}, status_code=404
        )

    if is_rendered_jsonl:
        content = _render_log_content(source.render, content)

    return JSONResponse({
        "id": request.query_params.get("id", ""),
        "content": content,
        "size": size,
    })


def _render_log_content(render: str, content: str) -> str:
    if render == "openshrimp-agent-jsonl":
        return render_openshrimp_agent_content(content)
    if render == "jsonl":
        return render_jsonl_content(content)
    return content


def _render_log_lines(render: str, content: str) -> tuple[str, str]:
    if render == "openshrimp-agent-jsonl":
        return render_openshrimp_agent_lines(content)
    if render == "jsonl":
        return render_jsonl_lines(content)
    return content, ""


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
