"""HTTP/WebSocket API routes for the VNC Mini App.

Provides a WebSocket-to-TCP proxy for connecting noVNC clients to
the wayvnc server running inside computer-use containers.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from open_udang.config import Config
from open_udang.container import get_vnc_port
from open_udang.review.auth import AuthError, validate_init_data

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

    # Authenticate via Telegram initData passed as query param.
    try:
        await validate_init_data(
            f"tg-init-data {token}",
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
