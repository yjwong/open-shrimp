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


async def submit_review_endpoint(request: Request) -> JSONResponse:
    """POST /api/preview/submit-review — submit document review comments.

    Formats the comments into a prompt and dispatches it to the agent
    for the given chat via the dispatch registry.

    Expects JSON body::

        {
            "chat_id": <int>,
            "thread_id": <int|null>,
            "path": <str>,
            "comments": [{"block_text": <str>, "comment": <str>}, ...]
        }
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Validate chat_id.
    try:
        chat_id = int(body["chat_id"])
    except (KeyError, ValueError, TypeError):
        return JSONResponse(
            {"error": "chat_id is required (integer)"}, status_code=400
        )

    # Validate thread_id (optional).
    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    # Validate path.
    path_str = body.get("path", "")
    if not path_str or not isinstance(path_str, str):
        return JSONResponse(
            {"error": "path is required (string)"}, status_code=400
        )

    file_path = Path(path_str)
    if not file_path.is_absolute():
        return JSONResponse(
            {"error": "path must be absolute"}, status_code=400
        )

    config: Config = request.app.state.config
    if not _is_within_context_directories(file_path, config):
        return JSONResponse(
            {"error": "path is outside configured context directories"},
            status_code=403,
        )

    # Validate comments.
    comments = body.get("comments")
    if not isinstance(comments, list) or not comments:
        return JSONResponse(
            {"error": "comments must be a non-empty array"}, status_code=400
        )

    if len(comments) > 50:
        return JSONResponse(
            {"error": "too many comments (max 50)"}, status_code=400
        )

    # Build the prompt.
    prompt_parts = [
        f"The user has reviewed the document at `{path_str}` and left "
        f"the following comments:"
    ]

    for i, entry in enumerate(comments, 1):
        if not isinstance(entry, dict):
            return JSONResponse(
                {"error": f"comment {i} must be an object"}, status_code=400
            )

        block_text = str(entry.get("block_text", ""))[:200]
        comment_text = str(entry.get("comment", ""))
        if not comment_text:
            return JSONResponse(
                {"error": f"comment {i} has empty comment text"}, status_code=400
            )
        if len(comment_text) > 2000:
            return JSONResponse(
                {"error": f"comment {i} text too long (max 2000 chars)"},
                status_code=400,
            )

        prompt_parts.append(f"\n### Comment {i}")
        if block_text:
            prompt_parts.append(f"> {block_text}")
        prompt_parts.append(f"\n{comment_text}")

    prompt = "\n".join(prompt_parts)

    # Dispatch.
    from open_udang.dispatch_registry import dispatch as dispatch_to_agent

    try:
        await dispatch_to_agent(
            prompt, chat_id, thread_id,
            placeholder="Reviewing document feedback\\.\\.\\.",
        )
    except RuntimeError as e:
        logger.error("submit_review_endpoint: %s", e)
        return JSONResponse(
            {"error": "Review dispatch not available — bot may not be running"},
            status_code=503,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch review for chat %d", chat_id
        )
        return JSONResponse(
            {"error": "Failed to dispatch review"}, status_code=500
        )

    return JSONResponse({"ok": True})


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
        Route("/api/preview/submit-review", submit_review_endpoint, methods=["POST"]),
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
