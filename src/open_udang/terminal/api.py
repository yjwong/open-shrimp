"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing background task output files
and a REST endpoint for reading task output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_udang.config import Config
from open_udang.container import CONTAINER_STATE_DIR
from open_udang.review.auth import AuthError, validate_init_data

logger = logging.getLogger(__name__)

# Task ID pattern: alphanumeric, used by Claude CLI (e.g. "brf4e7jzw")
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Base directory for Claude CLI tmp files
_CLAUDE_TMP_BASE = Path(f"/tmp/claude-{os.getuid()}")


def _search_tmp_base(base: Path, filename: str) -> Path | None:
    """Search a Claude CLI tmp base directory for a task output file.

    Looks for ``<base>/<project>/tasks/<filename>`` and
    ``<base>/<project>/<session>/tasks/<filename>``.
    """
    if not base.is_dir():
        return None

    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue

        candidate = project_dir / "tasks" / filename
        if candidate.is_file():
            return candidate

        for sub in project_dir.iterdir():
            if not sub.is_dir():
                continue
            candidate = sub / "tasks" / filename
            if candidate.is_file():
                return candidate

    return None


def _find_task_output_file(task_id: str) -> Path | None:
    """Find the output file for a background task by ID.

    Searches the host Claude CLI tmp directory and all container state
    directories (where containerized contexts write their tmp files).
    """
    if not _TASK_ID_RE.match(task_id):
        return None

    filename = f"{task_id}.output"

    # Search the host tmp directory first.
    result = _search_tmp_base(_CLAUDE_TMP_BASE, filename)
    if result:
        return result

    # Search container state directories: each has a tmp/ subdirectory
    # that is bind-mounted as /tmp/claude-<uid>/ inside the container.
    if CONTAINER_STATE_DIR.is_dir():
        for context_dir in CONTAINER_STATE_DIR.iterdir():
            tmp_dir = context_dir / "tmp"
            result = _search_tmp_base(tmp_dir, filename)
            if result:
                return result

    return None


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await validate_init_data(
        authorization, config.telegram.token, config.allowed_users
    )


async def tail_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """GET /api/terminal/tail — SSE stream tailing a task output file.

    Query params:
        task_id: The background task ID.
        offset: Byte offset to start reading from (default 0).
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    task_id = request.query_params.get("task_id", "")
    if not task_id or not _TASK_ID_RE.match(task_id):
        return JSONResponse(
            {"error": "task_id is required (alphanumeric)"}, status_code=400
        )

    offset = int(request.query_params.get("offset", "0"))
    if offset < 0:
        offset = 0

    output_file = _find_task_output_file(task_id)
    if output_file is None:
        return JSONResponse(
            {"error": f"Task output not found: {task_id}"}, status_code=404
        )

    async def event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events as the file grows."""
        pos = offset
        idle_count = 0
        # Maximum idle iterations before we consider the task done.
        # At 0.5s intervals, 120 iterations = 60 seconds of no new output.
        max_idle = 120

        while True:
            try:
                stat = output_file.stat()
                file_size = stat.st_size
            except FileNotFoundError:
                # File was deleted — task finished and cleaned up.
                yield "event: done\ndata: {}\n\n"
                return

            if file_size > pos:
                # New data available.
                idle_count = 0
                chunk = await asyncio.to_thread(
                    _read_chunk, output_file, pos, file_size
                )
                if chunk:
                    payload = json.dumps({
                        "text": chunk,
                        "offset": file_size,
                    })
                    yield f"data: {payload}\n\n"
                    pos = file_size
            else:
                idle_count += 1
                if idle_count >= max_idle:
                    yield "event: done\ndata: {}\n\n"
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
    """GET /api/terminal/read — read the full content of a task output file.

    Query params:
        task_id: The background task ID.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    task_id = request.query_params.get("task_id", "")
    if not task_id or not _TASK_ID_RE.match(task_id):
        return JSONResponse(
            {"error": "task_id is required (alphanumeric)"}, status_code=400
        )

    output_file = _find_task_output_file(task_id)
    if output_file is None:
        return JSONResponse(
            {"error": f"Task output not found: {task_id}"}, status_code=404
        )

    try:
        content = await asyncio.to_thread(output_file.read_text, "utf-8", "replace")
        size = output_file.stat().st_size
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"Task output not found: {task_id}"}, status_code=404
        )

    return JSONResponse({
        "task_id": task_id,
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
