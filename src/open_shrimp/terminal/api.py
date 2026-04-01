"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing log sources (background task output,
container build logs, etc.) and a REST endpoint for reading their content.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_shrimp.config import Config
from open_shrimp.review.auth import AuthError, validate_init_data
from open_shrimp.terminal.jsonl_render import render_jsonl_content, render_jsonl_lines
from open_shrimp.terminal.log_source import LogSource, resolve

logger = logging.getLogger(__name__)


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await validate_init_data(
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
    return resolve(source_type, source_id, task_type=task_type)


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
        """Generate SSE events as the file grows."""
        pos = offset
        idle_count = 0
        line_buffer = ""  # carries incomplete JSONL lines (agent only)
        # Maximum idle iterations before we consider the source done.
        # At 0.5s intervals, 120 iterations = 60 seconds of no new output.
        max_idle = 120
        # After we detect the source is no longer active, allow a few more
        # iterations to drain any remaining output before sending done.
        finishing = False
        finish_countdown = 6  # 3 seconds (6 × 0.5s)

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

        while True:
            try:
                stat = source.path.stat()
                file_size = stat.st_size
            except FileNotFoundError:
                # File was deleted — source finished and cleaned up.
                yield _flush_and_done(completed=True)
                return

            if file_size > pos:
                # New data available.
                idle_count = 0
                chunk = await asyncio.to_thread(
                    _read_chunk, source.path, pos, file_size
                )
                if chunk:
                    if is_agent:
                        text_to_render = line_buffer + chunk
                        rendered, line_buffer = render_jsonl_lines(
                            text_to_render
                        )
                        if rendered:
                            payload = json.dumps({
                                "text": rendered,
                                "offset": file_size,
                            })
                            yield f"data: {payload}\n\n"
                    else:
                        payload = json.dumps({
                            "text": chunk,
                            "offset": file_size,
                        })
                        yield f"data: {payload}\n\n"
                    pos = file_size
            else:
                # No new data — check if the source has finished.
                if not finishing and not source.is_active():
                    finishing = True
                    finish_countdown = 6

                if finishing:
                    finish_countdown -= 1
                    if finish_countdown <= 0:
                        yield _flush_and_done(completed=True)
                        return

                idle_count += 1
                if idle_count >= max_idle:
                    yield _flush_and_done(completed=False)
                    return

            await asyncio.sleep(0.5)

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
