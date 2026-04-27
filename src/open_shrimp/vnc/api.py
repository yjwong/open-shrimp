"""HTTP/WebSocket API routes for the VNC Mini App.

Provides a WebSocket-to-TCP proxy for connecting noVNC clients to
the VNC server running inside computer-use containers or VMs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
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
)
from open_shrimp.review.auth import AuthError, validate_token_param
from open_shrimp.sandbox.base import VNC_QUIRK_RFB_DROPS_SET_ENCODINGS
from open_shrimp.vnc.apple_dh import AppleDhAuthError, authenticate as apple_dh_authenticate
from open_shrimp.vnc.rfb_filter import RfbClientFilter, RfbFilterError

logger = logging.getLogger(__name__)


def _is_computer_use_context(ctx: ContextConfig) -> bool:
    """Check whether a context has computer-use enabled (any backend)."""
    if ctx.container is not None and ctx.container.computer_use:
        return True
    if ctx.sandbox is not None and ctx.sandbox.computer_use:
        return True
    return False


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


def _get_vnc_port_for_context(
    context_name: str,
    ctx: ContextConfig,
    sandbox_managers: dict[str, object] | None = None,
) -> int | None:
    """Discover the VNC port for a context via the sandbox protocol."""
    sandbox = _get_sandbox_for_context(context_name, ctx, sandbox_managers)
    if sandbox is not None:
        return sandbox.get_vnc_port()
    return None


class _WSBufferedReader:
    """Read fixed-size chunks from a WebSocket of unaligned binary frames."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._buf = bytearray()

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf.extend(await self._ws.receive_bytes())
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def leftover(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


async def _authenticate_to_server(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    credentials: tuple[str, str],
) -> None:
    """Complete the RFB handshake with the upstream VNC server.

    Reads the server greeting, negotiates RFB 3.8, and runs the
    Apple DH (security type 30) auth flow using *credentials*.
    Leaves the stream positioned just before ``ServerInit`` (the
    server is now waiting for ``ClientInit``).
    """
    greeting = await reader.readexactly(12)
    if not greeting.startswith(b"RFB "):
        raise AppleDhAuthError(f"unexpected server greeting {greeting!r}")
    writer.write(b"RFB 003.008\n")
    await writer.drain()

    # Read the security-types list (count byte + types).  RFB 3.8: if
    # count == 0, server sends a 4-byte reason length and a reason string.
    count = (await reader.readexactly(1))[0]
    if count == 0:
        (reason_len,) = struct.unpack("!I", await reader.readexactly(4))
        reason = (await reader.readexactly(reason_len)).decode(
            "utf-8", errors="replace",
        )
        raise AppleDhAuthError(f"server refused connection: {reason}")
    types = await reader.readexactly(count)
    if 30 not in types:
        raise AppleDhAuthError(
            f"server does not offer Apple DH (sec types: {list(types)})"
        )

    writer.write(bytes([30]))
    await writer.drain()
    username, password = credentials
    await apple_dh_authenticate(reader, writer, username, password)


async def _fake_no_auth_handshake_to_client(
    websocket: WebSocket,
    ws_reader: _WSBufferedReader,
) -> None:
    """Pretend to be a credential-less RFB server to the noVNC client.

    Sends an RFB 3.8 greeting, advertises only security type 1
    ("None"), and signals authentication success.  After this returns,
    the next bytes from the client are ``ClientInit`` and should be
    forwarded as-is to the upstream server.
    """
    await websocket.send_bytes(b"RFB 003.008\n")
    await ws_reader.readexactly(12)  # client RFB version (ignored).

    # One sec-type: 1 = None.
    await websocket.send_bytes(bytes([1, 1]))
    selected = await ws_reader.readexactly(1)
    if selected[0] != 1:
        raise AppleDhAuthError(
            f"client refused offered security type 1 (sent {selected[0]})"
        )

    # RFB 3.8 SecurityResult: 0 = OK.
    await websocket.send_bytes(b"\x00\x00\x00\x00")


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

    # Resolve the sandbox handle and discover VNC port + credentials.
    sandbox_managers = getattr(websocket.app.state, "sandbox_managers", None)
    sandbox = await asyncio.to_thread(
        _get_sandbox_for_context, context_name, ctx, sandbox_managers,
    )
    port = sandbox.get_vnc_port() if sandbox is not None else None
    if port is None:
        await websocket.close(
            code=4004, reason="VNC port not available (container not running?)"
        )
        return
    credentials = (
        await asyncio.to_thread(sandbox.get_vnc_credentials)
        if sandbox is not None else None
    )
    quirks = sandbox.get_vnc_quirks() if sandbox is not None else frozenset()

    # Accept the WebSocket handshake.
    await websocket.accept()

    # Open TCP connection to the VNC server.
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError as exc:
        logger.warning("Failed to connect to VNC at port %d: %s", port, exc)
        await websocket.close(code=4005, reason="Cannot connect to VNC server")
        return

    rfb_filter: RfbClientFilter | None = (
        RfbClientFilter()
        if VNC_QUIRK_RFB_DROPS_SET_ENCODINGS in quirks
        else None
    )
    logger.info(
        "VNC proxy connected: context=%s, port=%d, auth=%s, filter=%s",
        context_name, port,
        "apple-dh" if credentials else "passthrough",
        "rfb-strip" if rfb_filter is not None else "none",
    )

    ws_reader = _WSBufferedReader(websocket)
    if credentials is not None:
        try:
            await _authenticate_to_server(reader, writer, credentials)
            await _fake_no_auth_handshake_to_client(websocket, ws_reader)
        except (AppleDhAuthError, asyncio.IncompleteReadError) as exc:
            logger.warning(
                "VNC handshake interception failed for %s: %s",
                context_name, exc,
            )
            writer.close()
            await websocket.close(code=4006, reason="VNC authentication failed")
            return

    async def ws_to_tcp() -> None:
        """Forward WebSocket binary frames to TCP."""
        async def forward(data: bytes) -> None:
            chunk = rfb_filter.feed(data) if rfb_filter is not None else data
            if chunk:
                writer.write(chunk)
                await writer.drain()

        try:
            # Drain any bytes the client already sent past the fake handshake.
            leftover = ws_reader.leftover()
            if leftover:
                await forward(leftover)
            while True:
                await forward(await websocket.receive_bytes())
        except RfbFilterError as exc:
            logger.warning(
                "RFB filter aborted for %s: %s", context_name, exc,
            )
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
