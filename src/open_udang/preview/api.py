"""HTTP API routes for the markdown preview Mini App.

Provides an endpoint for reading markdown files within configured context
directories, and serves the preview frontend.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_udang.config import Config
from open_udang.review.auth import AuthError, validate_init_data

logger = logging.getLogger(__name__)


def _is_within_context_directories(path: Path, config: Config) -> bool:
    """Check that *path* is inside at least one configured context directory.

    Uses ``Path.resolve()`` on both sides so symlinks and ``..`` components
    are collapsed before comparison — this prevents path-traversal attacks.
    """
    resolved = path.resolve()
    for ctx in config.contexts.values():
        ctx_dir = Path(ctx.directory).resolve()
        if resolved == ctx_dir or ctx_dir in resolved.parents:
            return True
        for extra in ctx.additional_directories:
            extra_dir = Path(extra).resolve()
            if resolved == extra_dir or extra_dir in resolved.parents:
                return True
    return False


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await validate_init_data(
        authorization, config.telegram.token, config.allowed_users
    )


async def read_endpoint(request: Request) -> JSONResponse:
    """GET /api/preview/read — read a markdown file for preview.

    Query params:
        path: Absolute path to the markdown file.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    file_path_str = request.query_params.get("path", "")
    if not file_path_str:
        return JSONResponse(
            {"error": "path query parameter is required"}, status_code=400
        )

    file_path = Path(file_path_str)

    # Must be an absolute path.
    if not file_path.is_absolute():
        return JSONResponse(
            {"error": "path must be absolute"}, status_code=400
        )

    # Path traversal protection: must resolve to within a context directory.
    config: Config = request.app.state.config
    if not _is_within_context_directories(file_path, config):
        return JSONResponse(
            {"error": "path is outside configured context directories"},
            status_code=403,
        )

    resolved = file_path.resolve()

    if not resolved.is_file():
        return JSONResponse(
            {"error": "file not found"}, status_code=404
        )

    try:
        content = await asyncio.to_thread(resolved.read_text, "utf-8")
    except Exception:
        logger.exception("Failed to read preview file %s", resolved)
        return JSONResponse(
            {"error": "failed to read file"}, status_code=500
        )

    return JSONResponse({
        "path": str(resolved),
        "filename": resolved.name,
        "content": content,
    })


def create_preview_routes() -> list[Route | Mount]:
    """Create the routes for the preview API and Mini App frontend.

    Returns a list of routes to be added to the main Starlette app.
    """
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "web"
        / "markdown-app"
        / "dist"
    )
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount] = [
        Route("/api/preview/read", read_endpoint, methods=["GET"]),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount(
                "/preview",
                app=StaticFiles(directory=str(_dist_dir), html=True),
                name="markdown-app",
            )
        )
        logger.info("Serving markdown preview Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "Markdown preview Mini App dist directory not found at %s — "
            "run 'npm run build' in web/markdown-app/ to build the frontend",
            _dist_dir,
        )

    return routes
