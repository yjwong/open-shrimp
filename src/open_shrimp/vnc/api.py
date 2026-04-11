"""HTTP/WebSocket API routes for the VNC Mini App.

Provides a WebSocket-to-TCP proxy for connecting noVNC clients to
the VNC server running inside computer-use containers or VMs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from open_shrimp.config import Config, ContextConfig
from open_shrimp.sandbox.docker_helpers import (
    get_text_input_active,
    get_text_input_state_path,
    get_vnc_port as docker_get_vnc_port,
)
from open_shrimp.review.auth import AuthError, validate_token_param

logger = logging.getLogger(__name__)


def _is_computer_use_context(ctx: ContextConfig) -> bool:
    """Check whether a context has computer-use enabled (any backend)."""
    if ctx.container is not None and ctx.container.computer_use:
        return True
    if ctx.sandbox is not None and ctx.sandbox.computer_use:
        return True
    return False


def _get_vnc_port_for_context(
    context_name: str,
    ctx: ContextConfig,
    sandbox_managers: dict[str, object] | None = None,
) -> int | None:
    """Discover the VNC port for a context, supporting both backends.

    Docker: reads the VNC port from the container's state directory.
    Libvirt: parses ``domain.XMLDesc()`` for the auto-assigned VNC port.
    """
    if ctx.container is not None and ctx.container.computer_use:
        return docker_get_vnc_port(context_name)

    if ctx.sandbox is not None and ctx.sandbox.computer_use:
        # Libvirt backend — need the sandbox manager's connection.
        sandbox_manager = (sandbox_managers or {}).get(ctx.sandbox.backend)
        if sandbox_manager is not None:
            try:
                from open_shrimp.sandbox.libvirt_helpers import (
                    domain_name,
                    extract_vnc_port_from_xml,
                )
                dom_name = domain_name(context_name, getattr(
                    sandbox_manager, "instance_prefix", "openshrimp",
                ))
                conn = getattr(sandbox_manager, "_conn", None)
                if conn is not None:
                    import libvirt
                    try:
                        domain = conn.lookupByName(dom_name)
                        if domain.isActive():
                            return extract_vnc_port_from_xml(domain.XMLDesc(0))
                    except libvirt.libvirtError:
                        pass
            except ImportError:
                pass
    return None


async def vnc_ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket proxy: bridge noVNC client to container's VNC TCP port.

    Query params:
        context: Context name (must have computer_use enabled).
        token: Telegram initData for authentication.
    """
    config: Config = websocket.app.state.config
    token = websocket.query_params.get("token", "")
    context_name = websocket.query_params.get("context", "")

    # Authenticate via token query param (initData or HMAC token).
    try:
        await validate_token_param(
            token,
            config.telegram.token,
            config.allowed_users,
        )
    except AuthError:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # Validate context.
    if not context_name or context_name not in config.contexts:
        await websocket.close(code=4002, reason="Unknown context")
        return

    ctx = config.contexts[context_name]
    if not _is_computer_use_context(ctx):
        await websocket.close(
            code=4003, reason="Context does not have computer_use enabled"
        )
        return

    # Discover the VNC port.
    sandbox_managers = getattr(websocket.app.state, "sandbox_managers", None)
    port = await asyncio.to_thread(
        _get_vnc_port_for_context, context_name, ctx, sandbox_managers,
    )
    if port is None:
        await websocket.close(
            code=4004, reason="VNC port not available (container not running?)"
        )
        return

    # Accept the WebSocket handshake.
    await websocket.accept()

    # Open TCP connection to the VNC server.
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError as exc:
        logger.warning("Failed to connect to VNC at port %d: %s", port, exc)
        await websocket.close(code=4005, reason="Cannot connect to VNC server")
        return

    logger.info(
        "VNC proxy connected: context=%s, port=%d", context_name, port
    )

    async def ws_to_tcp() -> None:
        """Forward WebSocket binary frames to TCP."""
        try:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass

    async def tcp_to_ws() -> None:
        """Forward TCP data to WebSocket binary frames."""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass

    # Run both directions concurrently; when either ends, cancel the other.
    try:
        async with asyncio.TaskGroup() as tg:
            task_ws = tg.create_task(ws_to_tcp())
            task_tcp = tg.create_task(tcp_to_ws())
    except* Exception:
        pass
    finally:
        writer.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("VNC proxy disconnected: context=%s", context_name)


async def text_input_state_endpoint(request: Request) -> JSONResponse:
    """GET /api/vnc/text-input-state — text field focus state from container.

    Returns {"active": true/false} indicating whether a text input field
    is currently focused inside the computer-use container.  Used by the
    noVNC mobile client to auto-show/hide the soft keyboard.
    """
    config: Config = request.app.state.config
    token = request.query_params.get("token", "")
    context_name = request.query_params.get("context", "")

    try:
        await validate_token_param(
            token,
            config.telegram.token,
            config.allowed_users,
        )
    except AuthError:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not context_name or context_name not in config.contexts:
        return JSONResponse({"error": "Unknown context"}, status_code=400)

    ctx = config.contexts[context_name]
    if not _is_computer_use_context(ctx):
        return JSONResponse({"error": "Not a computer_use context"}, status_code=400)

    active = await asyncio.to_thread(get_text_input_active, context_name)
    return JSONResponse({"active": active})


async def text_input_state_stream_endpoint(
    request: Request,
) -> StreamingResponse | JSONResponse:
    """GET /api/vnc/text-input-state/stream — SSE stream of text-input focus.

    Pushes ``{"active": true/false}`` events whenever the text-input state
    changes inside the computer-use container, using inotify on the
    bind-mounted state file for instant notification.
    """
    config: Config = request.app.state.config
    token = request.query_params.get("token", "")
    context_name = request.query_params.get("context", "")

    try:
        await validate_token_param(
            token,
            config.telegram.token,
            config.allowed_users,
        )
    except AuthError:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not context_name or context_name not in config.contexts:
        return JSONResponse({"error": "Unknown context"}, status_code=400)

    ctx = config.contexts[context_name]
    if not _is_computer_use_context(ctx):
        return JSONResponse({"error": "Not a computer_use context"}, status_code=400)

    state_path = get_text_input_state_path(context_name)

    def _read_state() -> bool:
        try:
            return state_path.read_text(encoding="utf-8").strip() == "1"
        except (FileNotFoundError, OSError):
            return False

    async def event_stream() -> AsyncGenerator[str, None]:
        from watchfiles import awatch

        last_active = _read_state()
        yield f"data: {json.dumps({'active': last_active})}\n\n"

        # Detect client disconnect.
        stop_event = asyncio.Event()

        async def watch_disconnect() -> None:
            while not stop_event.is_set():
                if await request.is_disconnected():
                    stop_event.set()
                    return
                await asyncio.sleep(1)

        disconnect_task = asyncio.create_task(watch_disconnect())

        try:
            if not state_path.exists():
                # No bind mount (old container) — just keep connection open
                # with initial state until client disconnects.
                await stop_event.wait()
                return

            async for _changes in awatch(
                state_path, stop_event=stop_event
            ):
                active = _read_state()
                if active != last_active:
                    last_active = active
                    yield f"data: {json.dumps({'active': active})}\n\n"
        finally:
            stop_event.set()
            disconnect_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _get_sandbox_for_context(
    context_name: str,
    ctx: ContextConfig,
    sandbox_managers: dict[str, object] | None = None,
) -> object | None:
    """Get the cached Sandbox instance for a computer-use context."""
    backend: str | None = None
    if ctx.container is not None and ctx.container.computer_use:
        backend = "docker"
    elif ctx.sandbox is not None and ctx.sandbox.computer_use:
        backend = ctx.sandbox.backend

    if backend is None:
        return None

    manager = (sandbox_managers or {}).get(backend)
    if manager is None:
        return None

    create = getattr(manager, "create_sandbox", None)
    if create is None:
        return None

    return create(context_name, ctx)


async def clipboard_get_endpoint(request: Request) -> JSONResponse:
    """GET /api/vnc/clipboard — read the sandbox Wayland clipboard."""
    config: Config = request.app.state.config
    token = request.query_params.get("token", "")
    context_name = request.query_params.get("context", "")

    try:
        await validate_token_param(
            token, config.telegram.token, config.allowed_users,
        )
    except AuthError:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not context_name or context_name not in config.contexts:
        return JSONResponse({"error": "Unknown context"}, status_code=400)

    ctx = config.contexts[context_name]
    if not _is_computer_use_context(ctx):
        return JSONResponse(
            {"error": "Not a computer_use context"}, status_code=400,
        )

    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    sandbox = await asyncio.to_thread(
        _get_sandbox_for_context, context_name, ctx, sandbox_managers,
    )
    if sandbox is None:
        return JSONResponse(
            {"error": "Sandbox not available"}, status_code=503,
        )

    try:
        text = await asyncio.to_thread(sandbox.get_clipboard)
    except (NotImplementedError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"text": text})


async def clipboard_set_endpoint(request: Request) -> JSONResponse:
    """POST /api/vnc/clipboard — write to the sandbox Wayland clipboard."""
    config: Config = request.app.state.config
    token = request.query_params.get("token", "")
    context_name = request.query_params.get("context", "")

    try:
        await validate_token_param(
            token, config.telegram.token, config.allowed_users,
        )
    except AuthError:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not context_name or context_name not in config.contexts:
        return JSONResponse({"error": "Unknown context"}, status_code=400)

    ctx = config.contexts[context_name]
    if not _is_computer_use_context(ctx):
        return JSONResponse(
            {"error": "Not a computer_use context"}, status_code=400,
        )

    try:
        body = await request.json()
        text = body.get("text", "")
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    sandbox = await asyncio.to_thread(
        _get_sandbox_for_context, context_name, ctx, sandbox_managers,
    )
    if sandbox is None:
        return JSONResponse(
            {"error": "Sandbox not available"}, status_code=503,
        )

    try:
        await asyncio.to_thread(sandbox.set_clipboard, text)
    except (NotImplementedError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


def create_vnc_routes() -> list[Route | Mount | WebSocketRoute]:
    """Create the routes for the VNC API and Mini App frontend.

    Returns a list of routes to be added to the main Starlette app.
    """
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "web"
        / "vnc-app"
        / "dist"
    )
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount | WebSocketRoute] = [
        WebSocketRoute("/api/vnc/ws", vnc_ws_endpoint),
        Route("/api/vnc/clipboard", clipboard_get_endpoint, methods=["GET"]),
        Route("/api/vnc/clipboard", clipboard_set_endpoint, methods=["POST"]),
        Route("/api/vnc/text-input-state", text_input_state_endpoint),
        Route(
            "/api/vnc/text-input-state/stream",
            text_input_state_stream_endpoint,
        ),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount(
                "/vnc",
                app=StaticFiles(directory=str(_dist_dir), html=True),
                name="vnc-app",
            )
        )
        logger.info("Serving VNC Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "VNC Mini App dist directory not found at %s — "
            "run 'npm run build' in web/vnc-app/ to build the frontend",
            _dist_dir,
        )

    return routes
