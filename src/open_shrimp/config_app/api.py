"""HTTP API routes for the config Mini App.

Provides endpoints for reading and writing the OpenShrimp config,
authenticated via Telegram initData or HMAC token.

Uses ruamel.yaml round-trip loading so that comments and formatting in
the user's config file are preserved across edits.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_shrimp.client_manager import close_sessions_for_context
from open_shrimp.config import (
    Config,
    _validate_raw,
    config_to_dict,
    load_config,
    load_raw_yaml,
    write_raw_yaml,
)
from open_shrimp.review.auth import AuthError, authenticate
from open_shrimp.sandbox import SandboxManager

logger = logging.getLogger(__name__)


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await authenticate(
        authorization, config.telegram.token, config.allowed_users
    )


def _to_plain(obj: Any) -> Any:
    """Recursively convert ruamel.yaml CommentedMap/Seq to plain dicts/lists.

    ``_validate_raw`` uses ``isinstance(x, dict)`` checks that fail on
    ``CommentedMap`` unless we convert first.
    """
    if hasattr(obj, "items"):  # Mapping-like (CommentedMap, dict)
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _patch_raw_yaml(raw: Any, body: dict[str, Any]) -> None:
    """Patch a ruamel.yaml round-trip structure with changes from the API.

    Modifies *raw* in-place, replacing only the editable top-level keys
    (``contexts``, ``allowed_users``, ``default_context``) while leaving
    everything else (``telegram``, ``review``, comments) untouched.
    """
    if "allowed_users" in body:
        raw["allowed_users"] = body["allowed_users"]

    if "default_context" in body:
        raw["default_context"] = body["default_context"]

    if "contexts" in body:
        # Replace the contexts mapping entirely.  We can't do a simple
        # assignment because the incoming JSON dict is a plain dict and
        # would lose any ruamel comment structure.  But the user's
        # comments on individual context keys are lost anyway when the
        # frontend rewrites them — the important thing is that comments
        # on *other* top-level keys (telegram, review, etc.) survive.
        raw["contexts"] = body["contexts"]


async def config_get_endpoint(request: Request) -> JSONResponse:
    """GET /api/config -- return current config (without telegram token)."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    config: Config = request.app.state.config
    data = config_to_dict(config)

    # Strip the telegram token for security.
    data.pop("telegram", None)
    # Strip review config (not editable via this UI).
    data.pop("review", None)
    data.pop("instance_name", None)

    return JSONResponse(data)


async def config_put_endpoint(request: Request) -> JSONResponse:
    """PUT /api/config -- save updated config (comment-preserving)."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    config_path = getattr(request.app.state, "config_path", None)
    if not config_path:
        return JSONResponse(
            {"error": "Config path not available"}, status_code=500
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    old_config: Config = request.app.state.config
    old_context_names = set(old_config.contexts.keys())

    path = Path(config_path)

    # Load the existing file with ruamel.yaml to preserve comments.
    try:
        raw = load_raw_yaml(path)
    except Exception:
        logger.exception("Failed to load raw YAML from %s", config_path)
        return JSONResponse(
            {"error": "Failed to read existing config file"},
            status_code=500,
        )

    # Patch only the editable fields into the round-trip structure.
    _patch_raw_yaml(raw, body)

    # Validate the patched config (needs plain dicts for isinstance checks).
    try:
        _validate_raw(_to_plain(raw))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)

    # Write back, preserving comments on untouched sections.
    try:
        write_raw_yaml(path, raw)
    except OSError as e:
        logger.exception("Failed to write config to %s", config_path)
        return JSONResponse(
            {"error": f"Failed to write config: {e}"}, status_code=500
        )

    # Update app.state.config immediately so the next GET returns
    # fresh data (the watcher handles bot_data separately).
    try:
        new_config = load_config(config_path)
        request.app.state.config = new_config
    except Exception:
        logger.exception("Failed to reload config after write")
        return JSONResponse({"ok": True})

    new_context_names = set(new_config.contexts.keys())
    removed = old_context_names - new_context_names
    if removed:
        sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
        if sandbox_managers:
            from open_shrimp.sandbox.manager import destroy_contexts_background
            destroy_contexts_background(removed, sandbox_managers)

    return JSONResponse({"ok": True})


def _resolve_sandbox_manager(
    request: Request, context_name: str,
) -> tuple[SandboxManager | None, JSONResponse | None]:
    """Look up the SandboxManager for *context_name*.

    Returns ``(manager, None)`` on success, or ``(None, error_response)``
    if the context is missing, has no sandbox configured, or the manager
    for its backend isn't registered.
    """
    config: Config = request.app.state.config
    ctx = config.contexts.get(context_name)
    if ctx is None:
        return None, JSONResponse(
            {"error": f"Unknown context '{context_name}'"}, status_code=404,
        )
    if ctx.sandbox is None:
        return None, JSONResponse(
            {"error": f"Context '{context_name}' has no sandbox configured"},
            status_code=400,
        )

    sandbox_managers: dict[str, SandboxManager] | None = getattr(
        request.app.state, "sandbox_managers", None,
    )
    if not sandbox_managers:
        return None, JSONResponse(
            {"error": "Sandbox managers not available"}, status_code=500,
        )

    manager = sandbox_managers.get(ctx.sandbox.backend)
    if manager is None:
        return None, JSONResponse(
            {
                "error": (
                    f"No manager registered for backend "
                    f"'{ctx.sandbox.backend}'"
                ),
            },
            status_code=500,
        )

    return manager, None


async def sandbox_reboot_endpoint(request: Request) -> JSONResponse:
    """POST /api/sandbox/{context_name}/reboot -- stop + start, keep state."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    context_name = request.path_params["context_name"]
    manager, err = _resolve_sandbox_manager(request, context_name)
    if err is not None:
        return err
    assert manager is not None

    closed = await close_sessions_for_context(context_name)
    logger.info(
        "Rebooting sandbox for context '%s' (closed %d session(s))",
        context_name, closed,
    )
    try:
        await asyncio.to_thread(manager.invalidate_sandbox, context_name)
    except Exception as e:
        logger.exception(
            "Failed to reboot sandbox for context '%s'", context_name,
        )
        return JSONResponse(
            {"error": f"Failed to reboot sandbox: {e}"}, status_code=500,
        )

    return JSONResponse({"ok": True, "closed_sessions": closed})


async def sandbox_reset_endpoint(request: Request) -> JSONResponse:
    """POST /api/sandbox/{context_name}/reset -- destroy + recreate.

    Wipes overlays, state directories, Docker images, libvirt domains,
    and Lima instances.  Persistent volumes (libvirt) survive by design.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    context_name = request.path_params["context_name"]
    manager, err = _resolve_sandbox_manager(request, context_name)
    if err is not None:
        return err
    assert manager is not None

    closed = await close_sessions_for_context(context_name)
    logger.info(
        "Resetting sandbox for context '%s' (closed %d session(s))",
        context_name, closed,
    )
    try:
        await asyncio.to_thread(manager.destroy_context, context_name)
    except Exception as e:
        logger.exception(
            "Failed to reset sandbox for context '%s'", context_name,
        )
        return JSONResponse(
            {"error": f"Failed to reset sandbox: {e}"}, status_code=500,
        )

    return JSONResponse({"ok": True, "closed_sessions": closed})


async def validate_path_endpoint(request: Request) -> JSONResponse:
    """POST /api/config/validate-path -- check if a directory exists."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path_str = body.get("path", "")
    if not path_str:
        return JSONResponse(
            {"error": "path is required"}, status_code=400
        )

    p = Path(path_str).expanduser()
    exists = p.is_dir()

    return JSONResponse({
        "exists": exists,
        "path": str(p.resolve()) if exists else str(p),
    })


def create_config_routes() -> list[Route | Mount]:
    """Create routes for the config Mini App API and frontend."""
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "web"
        / "config-app"
        / "dist"
    )
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount] = [
        Route("/api/config", config_get_endpoint, methods=["GET"]),
        Route("/api/config", config_put_endpoint, methods=["PUT"]),
        Route(
            "/api/config/validate-path",
            validate_path_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/sandbox/{context_name}/reboot",
            sandbox_reboot_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/sandbox/{context_name}/reset",
            sandbox_reset_endpoint,
            methods=["POST"],
        ),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount(
                "/config",
                app=StaticFiles(directory=str(_dist_dir), html=True),
                name="config-app",
            )
        )
        logger.info("Serving config Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "Config Mini App dist directory not found at %s -- "
            "run 'npm run build' in web/config-app/ to build the frontend",
            _dist_dir,
        )

    return routes
