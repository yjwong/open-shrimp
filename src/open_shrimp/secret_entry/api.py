"""HTTP routes for the secret-entry Mini App, and the file write behind them.

The ``request_secret`` tool opens a request naming a path inside the
context's directories; the user opens the form, and the submitted value is
written straight to that path.  The value travels browser → tunnel → this
process → disk: Telegram only carries the button, and the agent only learns
that the file was written.  Request bodies are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from open_shrimp.config import Config
from open_shrimp.hooks import is_path_within_any_directory
from open_shrimp.review.auth import AuthError, authenticate

logger = logging.getLogger(__name__)

_PAGE = Path(__file__).resolve().parent / "page.html"

# Large enough for a PEM private key or a service-account JSON.
MAX_SECRET_BYTES = 64 * 1024

ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Values matching this are written unquoted; anything else is quoted.
_ENV_BARE_RE = re.compile(r"[A-Za-z0-9_./:@+,%^=-]*")


@dataclass
class SecretRequest:
    """One pending request, resolved ``True`` on write, ``False`` on cancel."""

    path: str
    env_key: str | None
    description: str
    directories: list[str]
    outcome: asyncio.Future[bool]


_requests: dict[str, SecretRequest] = {}


def open_request(
    path: str,
    env_key: str | None,
    description: str,
    directories: list[str],
) -> tuple[str, SecretRequest]:
    """Register a request and return its unguessable id."""
    request_id = secrets.token_urlsafe(16)
    req = SecretRequest(
        path=path,
        env_key=env_key,
        description=description,
        directories=directories,
        outcome=asyncio.get_running_loop().create_future(),
    )
    _requests[request_id] = req
    return request_id, req


def close_request(request_id: str) -> None:
    """Forget a request; the form then answers 404 for it."""
    _requests.pop(request_id, None)


def _env_value(value: str) -> str:
    """Quote *value* for a ``KEY=value`` line read by dotenv or ``source``."""
    if _ENV_BARE_RE.fullmatch(value):
        return value
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def upsert_env(existing: str, key: str, value: str) -> str:
    """Set *key* in dotenv text, replacing the first assignment in place.

    Later assignments of the same key are dropped, since dotenv readers let
    the last one win and it would shadow the new value.  An ``export``
    prefix on the replaced line is kept.
    """
    pattern = re.compile(rf"^(\s*(?:export\s+)?){re.escape(key)}\s*=")
    out: list[str] = []
    replaced = False
    for line in existing.splitlines(keepends=True):
        m = pattern.match(line)
        if m is None:
            out.append(line)
        elif not replaced:
            out.append(f"{m.group(1)}{key}={_env_value(value)}\n")
            replaced = True
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"{key}={_env_value(value)}\n")
    return "".join(out)


def write_secret(path: str, value: str, env_key: str | None) -> None:
    """Write *value* to *path*, atomically, readable by its owner only.

    With *env_key* the value becomes a ``KEY=value`` line in a dotenv file;
    without, it is the whole file, byte for byte.  A new file is created
    0600 (missing parent directories 0700); an existing one keeps its mode.
    """
    target = Path(path)
    data = value
    if env_key is not None:
        existing = target.read_text("utf-8") if target.exists() else ""
        data = upsert_env(existing, env_key, value)

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def _authenticate(request: Request) -> None:
    config: Config = request.app.state.config
    await authenticate(
        request.headers.get("authorization", ""),
        config.telegram.token,
        config.allowed_users,
    )


def _pending(request_id: str) -> SecretRequest | None:
    req = _requests.get(request_id)
    if req is None or req.outcome.done():
        return None
    return req


_GONE = {"error": "This request has expired or was already answered."}


async def describe_endpoint(request: Request) -> JSONResponse:
    """GET /api/secret/{request_id} — what the form is about to write."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    req = _pending(request.path_params["request_id"])
    if req is None:
        return JSONResponse(_GONE, status_code=404)
    return JSONResponse({
        "path": req.path,
        "env_key": req.env_key,
        "description": req.description,
        "exists": os.path.exists(req.path),
    })


async def submit_endpoint(request: Request) -> JSONResponse:
    """POST /api/secret/{request_id} — write ``{"value": ...}`` to the file."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    request_id = request.path_params["request_id"]
    req = _pending(request_id)
    if req is None:
        return JSONResponse(_GONE, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or not value:
        return JSONResponse({"error": "Enter a value."}, status_code=400)
    if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
        return JSONResponse(
            {"error": f"Too long (max {MAX_SECRET_BYTES // 1024} KB)."},
            status_code=400,
        )
    if req.env_key is not None and ("\n" in value or "\r" in value):
        return JSONResponse(
            {"error": "A .env value must fit on one line."}, status_code=400,
        )

    # The path was checked when the request opened, but the agent can still
    # swap a directory for a symlink in between.
    if not is_path_within_any_directory(req.path, req.directories):
        return JSONResponse(
            {"error": "The path now points outside the context directory."},
            status_code=403,
        )

    # Taken out of the table before the write, so a second submit racing
    # this one finds nothing to answer.
    _requests.pop(request_id, None)
    try:
        await asyncio.to_thread(write_secret, req.path, value, req.env_key)
    except OSError as e:
        _requests[request_id] = req
        logger.warning("Secret write to %s failed: %s", req.path, e.strerror)
        return JSONResponse(
            {"error": f"Could not write the file: {e.strerror}"},
            status_code=500,
        )

    if not req.outcome.done():
        req.outcome.set_result(True)
    logger.info("Secret written to %s", req.path)
    return JSONResponse({"ok": True})


async def cancel_endpoint(request: Request) -> JSONResponse:
    """POST /api/secret/{request_id}/cancel — decline without writing."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    req = _pending(request.path_params["request_id"])
    if req is not None:
        req.outcome.set_result(False)
    return JSONResponse({"ok": True})


async def page_endpoint(request: Request) -> FileResponse:
    """GET /secret/ — the form.  Auth happens on its API calls."""
    return FileResponse(
        str(_PAGE),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def create_secret_routes() -> list[Route]:
    return [
        Route("/secret/", page_endpoint, methods=["GET"]),
        Route("/api/secret/{request_id}", describe_endpoint, methods=["GET"]),
        Route("/api/secret/{request_id}", submit_endpoint, methods=["POST"]),
        Route(
            "/api/secret/{request_id}/cancel", cancel_endpoint, methods=["POST"],
        ),
    ]
