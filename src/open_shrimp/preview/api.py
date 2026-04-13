"""HTTP API routes for the markdown preview Mini App.

Provides an endpoint for reading markdown files within configured context
directories, and serves the preview frontend.  Also supports ephemeral
content (e.g. plan text from ExitPlanMode) served by ID.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_shrimp.config import Config
from open_shrimp.review.auth import AuthError, authenticate, validate_token_param

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ephemeral content store — serves markdown content by ID for the Mini App.
# Entries expire after _CONTENT_TTL seconds and are lazily purged.
# ---------------------------------------------------------------------------

_CONTENT_TTL = 3600  # 1 hour

# {content_id: {"title": str, "content": str, "created": float}}
_content_store: dict[str, dict[str, Any]] = {}


def store_ephemeral_content(
    title: str,
    content: str,
    chat_id: int | None = None,
    thread_id: int | None = None,
    tool_use_id: str | None = None,
) -> str:
    """Store markdown content and return a unique ID for retrieval.

    When *chat_id*, *thread_id*, and *tool_use_id* are provided the entry
    can later be used to auto-deny a pending approval and dispatch review
    comments back to the agent.
    """
    # Lazy purge of expired entries.
    now = time.monotonic()
    expired = [k for k, v in _content_store.items() if now - v["created"] > _CONTENT_TTL]
    for k in expired:
        del _content_store[k]

    content_id = uuid.uuid4().hex[:12]
    _content_store[content_id] = {
        "title": title,
        "content": content,
        "created": now,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "tool_use_id": tool_use_id,
    }
    return content_id


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
    return await authenticate(
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


async def content_endpoint(request: Request) -> JSONResponse:
    """GET /api/preview/content/{content_id} — read ephemeral content by ID."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    content_id = request.path_params.get("content_id", "")
    entry = _content_store.get(content_id)
    if not entry:
        return JSONResponse({"error": "content not found or expired"}, status_code=404)

    if time.monotonic() - entry["created"] > _CONTENT_TTL:
        _content_store.pop(content_id, None)
        return JSONResponse({"error": "content expired"}, status_code=404)

    return JSONResponse({
        "filename": entry["title"],
        "content": entry["content"],
    })


_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif",
}


async def image_endpoint(request: Request) -> JSONResponse | FileResponse:
    """GET /api/preview/image — serve an image file from disk.

    Query params:
        path: Absolute path to the image file.
        token: Optional auth token (for ``<img>`` tags that can't send headers).
    """
    config: Config = request.app.state.config
    token = request.query_params.get("token", "")
    try:
        if token:
            await validate_token_param(
                token, config.telegram.token, config.allowed_users
            )
        else:
            await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    file_path_str = request.query_params.get("path", "")
    if not file_path_str:
        return JSONResponse(
            {"error": "path query parameter is required"}, status_code=400
        )

    file_path = Path(file_path_str)

    if not file_path.is_absolute():
        return JSONResponse(
            {"error": "path must be absolute"}, status_code=400
        )

    if not _is_within_context_directories(file_path, config):
        return JSONResponse(
            {"error": "path is outside configured context directories"},
            status_code=403,
        )

    resolved = file_path.resolve()

    if resolved.suffix.lower() not in _IMAGE_EXTENSIONS:
        return JSONResponse(
            {"error": "not a supported image file"}, status_code=400
        )

    if not resolved.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)

    media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return FileResponse(str(resolved), media_type=media_type)


async def submit_review_endpoint(request: Request) -> JSONResponse:
    """POST /api/preview/submit-review — submit document review comments.

    Formats the comments into a prompt and dispatches it to the agent
    for the given chat via the dispatch registry.

    Expects JSON body — **either** file-based or ephemeral-content-based::

        # File-based (existing behaviour):
        {
            "chat_id": <int>,
            "thread_id": <int|null>,
            "path": <str>,
            "comments": [{"block_text": <str>, "comment": <str>}, ...]
        }

        # Ephemeral content (plan review):
        {
            "content_id": <str>,
            "comments": [{"block_text": <str>, "comment": <str>}, ...]
        }

    When ``content_id`` is provided, the chat/thread targeting and
    ``tool_use_id`` are read from the ephemeral content store, and the
    pending ExitPlanMode approval is automatically denied.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # ---- Resolve target (file-based vs ephemeral content) ----
    content_id = body.get("content_id")
    tool_use_id: str | None = None

    if content_id:
        # Ephemeral content mode (e.g. plan review).
        entry = _content_store.get(content_id)
        if not entry:
            return JSONResponse(
                {"error": "content not found or expired"}, status_code=404
            )
        if time.monotonic() - entry["created"] > _CONTENT_TTL:
            _content_store.pop(content_id, None)
            return JSONResponse({"error": "content expired"}, status_code=404)

        chat_id = entry.get("chat_id")
        thread_id = entry.get("thread_id")
        tool_use_id = entry.get("tool_use_id")

        if chat_id is None:
            return JSONResponse(
                {"error": "ephemeral content has no associated chat"},
                status_code=400,
            )

        subject = "your plan"
    else:
        # File-based mode (original behaviour).
        try:
            chat_id = int(body["chat_id"])
        except (KeyError, ValueError, TypeError):
            return JSONResponse(
                {"error": "chat_id is required (integer)"}, status_code=400
            )

        thread_id_raw = body.get("thread_id")
        thread_id = int(thread_id_raw) if thread_id_raw is not None else None

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

        subject = f"the document at `{path_str}`"

    # ---- Validate comments ----
    comments = body.get("comments")
    if not isinstance(comments, list) or not comments:
        return JSONResponse(
            {"error": "comments must be a non-empty array"}, status_code=400
        )

    if len(comments) > 50:
        return JSONResponse(
            {"error": "too many comments (max 50)"}, status_code=400
        )

    # ---- Build the prompt ----
    prompt_parts = [
        f"The user has reviewed {subject} and left the following comments:"
    ]

    for i, entry_c in enumerate(comments, 1):
        if not isinstance(entry_c, dict):
            return JSONResponse(
                {"error": f"comment {i} must be an object"}, status_code=400
            )

        block_text = str(entry_c.get("block_text", ""))[:200]
        comment_text = str(entry_c.get("comment", ""))
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

    # ---- Auto-deny pending ExitPlanMode approval ----
    if tool_use_id:
        await _auto_deny_plan_approval(request, tool_use_id)

    # ---- Dispatch ----
    from open_shrimp.dispatch_registry import dispatch as dispatch_to_agent

    try:
        await dispatch_to_agent(
            prompt, chat_id, thread_id,
            placeholder="Reviewing feedback\\.\\.\\.",
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


async def _auto_deny_plan_approval(
    request: Request, tool_use_id: str
) -> None:
    """Auto-deny a pending ExitPlanMode approval and update the Telegram message.

    Called when the user submits review comments on a plan, which implicitly
    means they are rejecting it.  We must read metadata *before* resolving
    the future because the ``finally`` block in ``_send_approval_keyboard``
    cleans up ``_approval_metadata`` immediately after resolution.
    """
    from open_shrimp.handlers.state import _approval_futures, _approval_metadata

    deny_key = f"deny:{tool_use_id}"
    future = _approval_futures.get(deny_key)
    if not future or future.done():
        logger.debug(
            "Plan approval for %s already resolved — skipping auto-deny",
            tool_use_id,
        )
        return

    # Read metadata before resolving (cleanup runs in the finally block).
    meta = _approval_metadata.get(tool_use_id, {})
    message_id = meta.get("message_id")
    approval_chat_id = meta.get("chat_id")

    # Deny the pending approval.
    future.set_result(False)

    # Edit the Telegram message to indicate it was denied via review.
    if message_id and approval_chat_id:
        try:
            from telegram import Bot

            config: Config = request.app.state.config
            bot = Bot(token=config.telegram.token)
            async with bot:
                status = (
                    "\n\n\u274c *Denied\\.* "
                    "_Review comments submitted\\._"
                )
                await bot.edit_message_text(
                    chat_id=approval_chat_id,
                    message_id=message_id,
                    text="\U0001f4cb *Plan*" + status,
                    parse_mode="MarkdownV2",
                )
        except Exception:
            logger.exception(
                "Failed to update Telegram message for auto-denied plan"
            )


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
        Route("/api/preview/content/{content_id}", content_endpoint, methods=["GET"]),
        Route("/api/preview/image", image_endpoint, methods=["GET"]),
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
