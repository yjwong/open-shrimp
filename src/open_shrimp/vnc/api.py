"""HTTP/WebSocket API routes for the VNC Mini App.

Provides a WebSocket-to-TCP proxy for connecting noVNC clients to
the wayvnc server running inside computer-use containers.
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

from open_shrimp.config import Config
from open_shrimp.sandbox.docker_helpers import (
    get_text_input_active,
    get_text_input_state_path,
    get_vnc_port,
)
from open_shrimp.review.auth import AuthError, validate_token_param

logger = logging.getLogger(__name__)


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
    if ctx.container is None or not ctx.container.computer_use:
        await websocket.close(
            code=4003, reason="Context does not have computer_use enabled"
        )
        return

    # Discover the VNC port.
    port = await asyncio.to_thread(get_vnc_port, context_name)
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
    if ctx.container is None or not ctx.container.computer_use:
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
    if ctx.container is None or not ctx.container.computer_use:
        return JSONResponse({"error": "Not a computer_use context"}, status_code=400)

    state_path = get_text_input_state_path(context_name)

    def _read_state() -> bool:
        try:
            return state_path.read_text().strip() == "1"
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
