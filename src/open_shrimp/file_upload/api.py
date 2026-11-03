"""HTTP routes for the upload Mini App, and the sessions behind them.

A button opens an upload session bound to one chat scope.  The page's first
call authenticates with Telegram initData (or the forum-chat token) and gets
back an upload key; every later call carries the key instead, because
initData and the token both expire an hour after the button was made and an
upload over a slow link can take longer.  The key only ever travels in a
response body and a request header, never in a URL, so Telegram never sees
it.

Files are written straight into the chat's upload directory in chunks of at
most :data:`CHUNK_BYTES`, well under Cloudflare's 100 MB request-body cap.  A
chunk is buffered and written only once it has fully arrived, so a dropped
connection leaves the file at the last whole chunk and the page resumes from
there.  On send the files become a turn exactly like Telegram attachments:
copied into the guest when the context is sandboxed, and deleted when the
turn ends.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import mimetypes
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from telegram import InlineKeyboardMarkup

from open_shrimp.agent import FileAttachment, create_attachment_file
from open_shrimp.config import Config
from open_shrimp.hooks import ATTACHMENT_TEMP_DIR
from open_shrimp.mini_app import mini_app_keyboard
from open_shrimp.review.auth import (
    AuthError,
    authenticate_request,
    read_json_body,
    reject_oversized_body,
)
from open_shrimp.web_url import mini_app_base

logger = logging.getLogger(__name__)

_PAGE = Path(__file__).resolve().parent / "page.html"

CHUNK_BYTES = 8 * 1024 * 1024

# A session with no request for this long is dropped with its files.
_SESSION_IDLE = 3600

_MAX_FILES = 10

# Room left on the upload directory's filesystem after every pending file
# has arrived.  /tmp is often a tmpfs, so this is also memory.
_FREE_SPACE_MARGIN = 512 * 1024 * 1024

# Telegram's own cap on a message, kept for the message sent with the files.
_MAX_MESSAGE_CHARS = 4096


@dataclass
class _File:
    name: str
    size: int
    mime_type: str
    path: Path
    received: int = 0


@dataclass
class UploadSession:
    chat_id: int
    thread_id: int | None
    caption: str
    key: str | None = None
    files: dict[str, _File] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: dict[str, UploadSession] = {}


def open_session(chat_id: int, thread_id: int | None, caption: str = "") -> str:
    """Start an upload session for a scope and return its id.

    *caption* prefills the page's message field, carrying over the text of
    a Telegram message whose attachment was too large to download.
    """
    _purge_idle()
    session_id = secrets.token_urlsafe(16)
    _sessions[session_id] = UploadSession(chat_id, thread_id, caption)
    return session_id


def upload_keyboard(
    config: Config,
    chat_id: int,
    thread_id: int | None,
    *,
    user_id: int,
    is_private_chat: bool,
    caption: str = "",
) -> InlineKeyboardMarkup | None:
    """An "Upload file" button for the scope, or ``None`` with no Mini App base."""
    if mini_app_base(config) is None:
        return None
    session_id = open_session(chat_id, thread_id, caption)
    return mini_app_keyboard(
        [("📤 Upload file", f"/upload/?id={session_id}")],
        config=config,
        chat_id=chat_id,
        user_id=user_id,
        is_private_chat=is_private_chat,
    )


def _delete_files(session: UploadSession) -> None:
    for f in session.files.values():
        f.path.unlink(missing_ok=True)
    session.files.clear()


def _discard(session_id: str) -> None:
    session = _sessions.pop(session_id, None)
    if session is not None:
        _delete_files(session)


def _purge_idle() -> None:
    now = time.monotonic()
    for session_id in [
        k for k, s in _sessions.items() if now - s.touched > _SESSION_IDLE
    ]:
        _discard(session_id)


def _pending_bytes() -> int:
    return sum(
        f.size - f.received for s in _sessions.values() for f in s.files.values()
    )


def _write_at(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b") as fh:
        fh.seek(offset)
        fh.write(data)


_GONE = "This upload has expired or was already sent. Ask for a new upload button."


def _keyed(request: Request) -> UploadSession:
    """The session a keyed request belongs to, after checking its key."""
    _purge_idle()
    session = _sessions.get(request.path_params["session_id"])
    key = request.headers.get("x-upload-key", "")
    if (
        session is None
        or session.key is None
        or not hmac.compare_digest(session.key, key)
    ):
        raise AuthError(404, _GONE)
    session.touched = time.monotonic()
    return session


def _file(session: UploadSession, request: Request) -> _File:
    f = session.files.get(request.path_params["file_id"])
    if f is None:
        raise AuthError(404, "Unknown file.")
    return f


def _error(e: AuthError) -> JSONResponse:
    return JSONResponse({"error": e.message}, status_code=e.status_code)


async def start_endpoint(request: Request) -> JSONResponse:
    """POST /api/upload/{session_id}/start — exchange Telegram auth for a key.

    Opening the page again issues a new key and drops files the previous
    page left half-sent, since a new page cannot resume them.
    """
    try:
        await authenticate_request(request)
    except AuthError as e:
        return _error(e)
    _purge_idle()
    session = _sessions.get(request.path_params["session_id"])
    if session is None:
        return JSONResponse({"error": _GONE}, status_code=404)
    async with session.lock:
        _delete_files(session)
        session.key = secrets.token_urlsafe(24)
        session.touched = time.monotonic()
    return JSONResponse({
        "key": session.key,
        "caption": session.caption,
        "chunk_bytes": CHUNK_BYTES,
    })


async def create_file_endpoint(request: Request) -> JSONResponse:
    """POST /api/upload/{session_id}/files — declare ``{"name", "size"}``."""
    try:
        session = _keyed(request)
        body = await read_json_body(request)
    except AuthError as e:
        return _error(e)

    name = body.get("name")
    size = body.get("size")
    if not isinstance(name, str) or not name or len(name) > 255:
        return JSONResponse({"error": "Invalid file name."}, status_code=400)
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        return JSONResponse({"error": f"{name} is empty."}, status_code=400)

    async with session.lock:
        if len(session.files) >= _MAX_FILES:
            return JSONResponse(
                {"error": f"At most {_MAX_FILES} files per upload."},
                status_code=400,
            )
        ATTACHMENT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(ATTACHMENT_TEMP_DIR).free
        if size + _pending_bytes() + _FREE_SPACE_MARGIN > free:
            return JSONResponse(
                {
                    "error": (
                        f"Not enough space for {name}: the OpenShrimp machine "
                        f"has {free / 2**30:.1f} GB free in "
                        f"{ATTACHMENT_TEMP_DIR}."
                    )
                },
                status_code=507,
            )
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        path = await asyncio.to_thread(
            create_attachment_file, session.chat_id, name, mime_type,
        )
        file_id = secrets.token_urlsafe(8)
        session.files[file_id] = _File(name, size, mime_type, path)
    logger.info("Upload of %s (%d bytes) started at %s", name, size, path)
    return JSONResponse({"file_id": file_id})


async def chunk_endpoint(request: Request) -> Response:
    """PUT /api/upload/{session_id}/files/{file_id}?offset=N — append a chunk.

    *offset* must equal the bytes already received; otherwise the reply is
    409 with the server's count, which the page resumes from.
    """
    try:
        session = _keyed(request)
        f = _file(session, request)
        reject_oversized_body(request, CHUNK_BYTES)
    except AuthError as e:
        return _error(e)
    try:
        offset = int(request.query_params.get("offset", ""))
    except ValueError:
        return JSONResponse({"error": "offset is required"}, status_code=400)

    data = bytearray()
    try:
        async for piece in request.stream():
            data += piece
            if len(data) > CHUNK_BYTES:
                return JSONResponse(
                    {"error": f"A chunk may be at most {CHUNK_BYTES} bytes."},
                    status_code=413,
                )
    except ClientDisconnect:
        return Response(status_code=400)

    async with session.lock:
        if session.files.get(request.path_params["file_id"]) is not f:
            return JSONResponse({"error": "Unknown file."}, status_code=404)
        if offset != f.received:
            return JSONResponse({"received": f.received}, status_code=409)
        if f.received + len(data) > f.size:
            return JSONResponse(
                {"error": "More data than the declared size."}, status_code=400,
            )
        try:
            await asyncio.to_thread(_write_at, f.path, offset, bytes(data))
        except OSError as e:
            logger.warning("Upload write to %s failed: %s", f.path, e)
            return JSONResponse(
                {"error": f"Could not write the file: {e.strerror}"},
                status_code=500,
            )
        f.received += len(data)
    return JSONResponse({"received": f.received})


async def file_status_endpoint(request: Request) -> JSONResponse:
    """GET /api/upload/{session_id}/files/{file_id} — bytes received so far."""
    try:
        f = _file(_keyed(request), request)
    except AuthError as e:
        return _error(e)
    return JSONResponse({"received": f.received, "size": f.size})


async def delete_file_endpoint(request: Request) -> JSONResponse:
    """DELETE /api/upload/{session_id}/files/{file_id} — drop one file."""
    try:
        session = _keyed(request)
    except AuthError as e:
        return _error(e)
    async with session.lock:
        f = session.files.pop(request.path_params["file_id"], None)
        if f is not None:
            f.path.unlink(missing_ok=True)
    return JSONResponse({"ok": True})


async def cancel_endpoint(request: Request) -> JSONResponse:
    """POST /api/upload/{session_id}/cancel — drop the session and its files."""
    try:
        _keyed(request)
    except AuthError as e:
        return _error(e)
    _discard(request.path_params["session_id"])
    return JSONResponse({"ok": True})


async def send_endpoint(request: Request) -> JSONResponse:
    """POST /api/upload/{session_id}/send — hand the files to the agent.

    ``{"message": ...}`` is the text sent with them.  The session is single
    use: it is removed before dispatch, and restored if dispatch fails so
    the user can retry without uploading again.
    """
    try:
        session = _keyed(request)
        body = await read_json_body(request)
    except AuthError as e:
        return _error(e)
    message = body.get("message", "")
    if not isinstance(message, str) or len(message) > _MAX_MESSAGE_CHARS:
        return JSONResponse(
            {"error": f"The message may be at most {_MAX_MESSAGE_CHARS} characters."},
            status_code=400,
        )

    session_id = request.path_params["session_id"]
    async with session.lock:
        if _sessions.get(session_id) is not session:
            return JSONResponse({"error": _GONE}, status_code=404)
        if not session.files:
            return JSONResponse({"error": "Choose a file first."}, status_code=400)
        unfinished = [f.name for f in session.files.values() if f.received < f.size]
        if unfinished:
            return JSONResponse(
                {"error": f"Still uploading: {', '.join(unfinished)}"},
                status_code=409,
            )
        _sessions.pop(session_id)

    files = list(session.files.values())
    attachments = [
        FileAttachment(data=b"", mime_type=f.mime_type, filename=f.name, staged=f.path)
        for f in files
    ]
    prompt = message.strip() or (
        "What's in this file?" if len(files) == 1 else "What's in these files?"
    )

    from open_shrimp.dispatch_registry import dispatch
    from open_shrimp.supervisor import SupervisorDispatchRefused

    try:
        await dispatch(
            prompt, session.chat_id, session.thread_id, attachments=attachments,
        )
    except Exception as e:
        _sessions[session_id] = session
        if isinstance(e, SupervisorDispatchRefused):
            return JSONResponse({"error": str(e)}, status_code=409)
        if isinstance(e, RuntimeError):
            logger.error("upload send_endpoint: %s", e)
            return JSONResponse(
                {"error": "OpenShrimp isn't ready to take messages. Try again shortly."},
                status_code=503,
            )
        logger.exception("Failed to dispatch upload for chat %d", session.chat_id)
        return JSONResponse({"error": "Failed to send the files."}, status_code=500)

    logger.info(
        "Upload of %d file(s) (%d bytes) dispatched to chat %d",
        len(files), sum(f.size for f in files), session.chat_id,
    )
    return JSONResponse({"ok": True})


async def page_endpoint(request: Request) -> FileResponse:
    """GET /upload/ — the page.  Auth happens on its API calls."""
    return FileResponse(
        str(_PAGE),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def create_upload_routes() -> list[Route]:
    base = "/api/upload/{session_id}"
    return [
        Route("/upload/", page_endpoint, methods=["GET"]),
        Route(f"{base}/start", start_endpoint, methods=["POST"]),
        Route(f"{base}/files", create_file_endpoint, methods=["POST"]),
        Route(f"{base}/files/{{file_id}}", chunk_endpoint, methods=["PUT"]),
        Route(f"{base}/files/{{file_id}}", file_status_endpoint, methods=["GET"]),
        Route(f"{base}/files/{{file_id}}", delete_file_endpoint, methods=["DELETE"]),
        Route(f"{base}/cancel", cancel_endpoint, methods=["POST"]),
        Route(f"{base}/send", send_endpoint, methods=["POST"]),
    ]
