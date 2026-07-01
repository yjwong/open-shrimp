"""Starlette routes and the stream-multiplexing loop for phone port forwarding.

The phone runs a local ``ServerSocket`` and multiplexes every accepted
connection over one relay WebSocket using :mod:`port_relay.frames`.  On the
bot side, each ``OPEN`` frame dials ``127.0.0.1:<host_port>`` (already mapped
to the sandbox guest port by an existing ``port_forward`` ssh tunnel) and the
two halves are piped together.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.android_companion import (
    authenticate_android_request,
    get_or_create_server_id,
    list_active_android_push_devices,
)
from open_shrimp.android_push import FcmPushSender, get_push_sender
from open_shrimp.config import Config
from open_shrimp.db import ChatScope, get_active_context
from open_shrimp.review.auth import (
    AuthError,
    authenticate_request,
    bounded_int,
    read_json_body,
)
from open_shrimp.web_url import openshrimp_server_label, phone_websocket_base
from open_shrimp.port_relay.frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_KEEPALIVE,
    FRAME_OPEN,
    decode_frame,
    encode_frame,
)
from open_shrimp.port_relay.sessions import (
    PortRelaySession,
    PortRelaySessionError,
    PortRelaySessionRegistry,
)

logger = logging.getLogger(__name__)

DEFAULT_SESSION_LIFETIME_SECONDS = 3600
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
MAX_SESSION_LIFETIME_SECONDS = 86400
MAX_IDLE_TIMEOUT_SECONDS = 86400
DEFAULT_MAX_STREAMS = 64
STREAM_CHUNK_SIZE = 64 * 1024
# Bounds buffered outbound frames per connection so a slow phone applies
# backpressure to fast loopback reads instead of growing memory unboundedly.
OUTBOUND_QUEUE_MAX = 32

REASON_DISCONNECT = "disconnect"
REASON_TIMEOUT = "timeout"
REASON_CANCELLED = "cancelled"
TERMINAL_REASONS = frozenset({REASON_DISCONNECT, REASON_TIMEOUT, REASON_CANCELLED})

# Injectable so tests can point streams at a loopback echo server.
Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _registry(request_or_ws: Request | WebSocket) -> PortRelaySessionRegistry:
    registry = getattr(request_or_ws.app.state, "port_relay_registry", None)
    if registry is None:
        registry = PortRelaySessionRegistry()
        request_or_ws.app.state.port_relay_registry = registry
    return registry


def port_forward_label(config: Config, context_name: str, host_port: int) -> str:
    return f"{openshrimp_server_label(config)} {context_name} :{host_port}"


def phone_relay_url(config: Config, session: PortRelaySession) -> str:
    return (
        f"{phone_websocket_base(config)}/api/port-forward/sessions/{session.id}/phone"
        f"?token={session.phone_token}"
    )


async def notify_paired_device(
    db: Any, config: Config, push_sender: FcmPushSender, session: PortRelaySession
) -> str:
    """Best-effort FCM push so the phone can claim without polling. Never raises."""
    devices = await list_active_android_push_devices(db)
    if not devices:
        return "no_device"
    try:
        result = await push_sender.send_port_forward_request(
            device=devices[0],
            server_id=await get_or_create_server_id(db),
            session_id=session.id,
            label=session.label,
            host_port=session.host_port,
        )
    except Exception:
        logger.exception("Failed to send port-forward push")
        return "failed"
    return result.status


async def create_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/port-forward/sessions."""
    try:
        await authenticate_request(request)
        body = await read_json_body(request)
        chat_id = int(body["chat_id"])
        raw_thread_id = body.get("thread_id")
        thread_id = int(raw_thread_id) if raw_thread_id is not None else None
        host_port = int(body["host_port"])
        if not 0 < host_port < 65536:
            raise AuthError(400, "host_port must be between 1 and 65535")
        lifetime_seconds = bounded_int(
            body.get("lifetime_seconds"),
            default=DEFAULT_SESSION_LIFETIME_SECONDS,
            minimum=10,
            maximum=MAX_SESSION_LIFETIME_SECONDS,
            field="lifetime_seconds",
        )
        idle_timeout_seconds = bounded_int(
            body.get("idle_timeout_seconds"),
            default=DEFAULT_IDLE_TIMEOUT_SECONDS,
            minimum=10,
            maximum=MAX_IDLE_TIMEOUT_SECONDS,
            field="idle_timeout_seconds",
        )
    except KeyError as exc:
        return JSONResponse({"error": f"{exc.args[0]} is required"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "chat_id, thread_id and host_port must be integers"},
            status_code=400,
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    config: Config = request.app.state.config
    db = request.app.state.db
    scope = ChatScope(chat_id, thread_id)

    raw_context = body.get("context_name")
    if raw_context is None:
        context_name = await get_active_context(db, scope) or config.default_context
    elif isinstance(raw_context, str) and raw_context:
        context_name = raw_context
    else:
        return JSONResponse(
            {"error": "context_name must be a non-empty string"}, status_code=400
        )
    if context_name not in config.contexts:
        return JSONResponse(
            {"error": f"Context '{context_name}' not found"}, status_code=404
        )

    try:
        session = await _registry(request).create(
            chat_id=scope.chat_id,
            thread_id=scope.thread_id,
            context_name=context_name,
            host_port=host_port,
            label=port_forward_label(config, context_name, host_port),
            lifetime_seconds=lifetime_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    except PortRelaySessionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)

    push_status = await notify_paired_device(
        db, config, get_push_sender(request.app.state, config), session
    )

    return JSONResponse(
        {
            **session.public_dict(),
            "phone_url": phone_relay_url(config, session),
            "phone_token": session.phone_token,
            "push_status": push_status,
        },
        status_code=201,
    )


async def get_session_endpoint(request: Request) -> JSONResponse:
    """GET /api/port-forward/sessions/{session_id}."""
    try:
        await authenticate_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    session = await _registry(request).get(request.path_params["session_id"])
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(session.public_dict())


async def cancel_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/port-forward/sessions/{session_id}/cancel."""
    try:
        await authenticate_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    session_id = request.path_params["session_id"]
    registry = _registry(request)
    session = await registry.get(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    await session.close(REASON_CANCELLED)
    await registry.remove(session_id)
    return JSONResponse({"ok": True, "status": "cancelled"})


async def android_pending_sessions_endpoint(request: Request) -> JSONResponse:
    """GET /api/port-forward/android/pending-sessions."""
    try:
        device = await authenticate_android_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    sessions = await _registry(request).list_active()
    public = [
        {
            "id": s.id,
            "context_name": s.context_name,
            "host_port": s.host_port,
            "label": s.label,
            "status": s.status,
            "created_at": s.created_at,
            "expires_at": s.expires_at,
            "claimed_by_this_device": s.claimed_device_id == device["device_id"],
        }
        for s in sessions
        if s.claimed_device_id in (None, device["device_id"])
    ]
    return JSONResponse({"sessions": public})


async def android_claim_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/port-forward/android/sessions/{session_id}/claim."""
    try:
        device = await authenticate_android_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    session_id = request.path_params["session_id"]
    session = await _registry(request).get(session_id)
    if session is None or not session.is_active():
        return JSONResponse({"error": "Session is no longer active"}, status_code=404)
    try:
        await session.claim(device["device_id"])
    except PortRelaySessionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(
        {
            "session": session.public_dict(),
            "label": session.label,
            "phone_url": phone_relay_url(request.app.state.config, session),
        }
    )


class MuxConnection:
    """Multiplexes the phone WebSocket's logical TCP streams onto loopback sockets.

    All WebSocket sends go through a single bounded queue drained by one sender
    task: this serialises sends (Starlette WebSockets are not safe for
    concurrent sends) and bounds buffered bytes so a slow phone backpressures
    the loopback readers.  Stream teardown uses the ``writers`` dict pop as the
    single ownership token — whoever pops a stream's writer owns closing it and
    notifying the phone, so EOF and phone-initiated close never double up.
    """

    def __init__(
        self,
        websocket: WebSocket,
        session: PortRelaySession,
        *,
        connect: Connect | None = None,
        max_streams: int = DEFAULT_MAX_STREAMS,
        chunk_size: int = STREAM_CHUNK_SIZE,
        outbound_max: int = OUTBOUND_QUEUE_MAX,
    ) -> None:
        self.ws = websocket
        self.session = session
        self._connect: Connect = connect or asyncio.open_connection
        self.max_streams = max_streams
        self.chunk_size = chunk_size
        self.writers: dict[int, asyncio.StreamWriter] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self._outbound: asyncio.Queue[bytes | dict[str, Any]] = asyncio.Queue(
            maxsize=outbound_max
        )
        self._sender_task: asyncio.Task[None] | None = None

    async def run(self) -> str:
        self._sender_task = asyncio.create_task(self._sender())
        await self._send({"type": "ready"})
        try:
            while True:
                timeout = min(
                    self.session.idle_timeout_seconds,
                    max(1, self.session.remaining_seconds()),
                )
                message = await asyncio.wait_for(self.ws.receive(), timeout=timeout)
                result = await self._handle(message)
                if result is not None:
                    return result
        finally:
            await self._shutdown()

    async def _sender(self) -> None:
        while True:
            item = await self._outbound.get()
            if isinstance(item, (bytes, bytearray)):
                await self.ws.send_bytes(item)
            else:
                await self.ws.send_json(item)

    async def _send(self, item: bytes | dict[str, Any]) -> None:
        await self._outbound.put(item)

    async def _handle(self, message: dict[str, Any]) -> str | None:
        if message.get("type") == "websocket.disconnect":
            return REASON_DISCONNECT
        data = message.get("bytes")
        if data is not None:
            await self._handle_frame(data)
            return None
        text = message.get("text")
        if text:
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                return None
            if isinstance(control, dict) and control.get("type") == "cancel":
                return REASON_CANCELLED
        return None

    async def _handle_frame(self, data: bytes) -> None:
        try:
            frame_type, stream_id, payload = decode_frame(data)
        except ValueError:
            await self._send({"type": "error", "error": "invalid_frame"})
            return
        if frame_type == FRAME_OPEN:
            await self._open(stream_id)
        elif frame_type == FRAME_DATA:
            await self._data(stream_id, payload)
        elif frame_type == FRAME_CLOSE:
            await self._close(stream_id, notify=False)
        elif frame_type == FRAME_KEEPALIVE:
            return
        else:
            await self._send({"type": "error", "error": "unknown_frame_type"})

    async def _open(self, stream_id: int) -> None:
        if stream_id in self.writers:
            return
        if len(self.writers) >= self.max_streams:
            await self._send_close(stream_id)
            return
        try:
            reader, writer = await self._connect("127.0.0.1", self.session.host_port)
        except OSError:
            await self._send_close(stream_id)
            return
        self.writers[stream_id] = writer
        self.tasks[stream_id] = asyncio.create_task(self._pump(stream_id, reader))

    async def _data(self, stream_id: int, payload: bytes) -> None:
        writer = self.writers.get(stream_id)
        if writer is None:
            return
        try:
            writer.write(payload)
            await writer.drain()
        except OSError:
            await self._close(stream_id, notify=True)

    async def _pump(self, stream_id: int, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await reader.read(self.chunk_size)
                if not chunk:
                    break
                await self._send(encode_frame(FRAME_DATA, stream_id, chunk))
        except asyncio.CancelledError:
            raise
        except OSError:
            pass
        # EOF/error from the origin.  If we still own the writer, tear it down
        # and tell the phone; a concurrent _close that already popped it owns
        # cleanup instead (and would have cancelled us before reaching here).
        writer = self.writers.pop(stream_id, None)
        self.tasks.pop(stream_id, None)
        if writer is not None:
            writer.close()
            await self._send_close(stream_id)

    async def _close(self, stream_id: int, *, notify: bool) -> None:
        task = self.tasks.pop(stream_id, None)
        writer = self.writers.pop(stream_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if writer is not None:
            writer.close()
            if notify:
                await self._send_close(stream_id)

    async def _send_close(self, stream_id: int) -> None:
        await self._send(encode_frame(FRAME_CLOSE, stream_id))

    async def _shutdown(self) -> None:
        pump_tasks = list(self.tasks.values())
        for stream_id in list(self.writers.keys() | self.tasks.keys()):
            await self._close(stream_id, notify=False)
        if pump_tasks:
            await asyncio.gather(*pump_tasks, return_exceptions=True)
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)


async def phone_ws_endpoint(websocket: WebSocket) -> None:
    session_id = websocket.path_params["session_id"]
    token = websocket.query_params.get("token", "")
    registry = _registry(websocket)

    session = await registry.get(session_id)
    if session is None or not session.is_active():
        await websocket.close(code=4004, reason="Session not found")
        return
    if not token or not session.validate_token(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    try:
        await session.attach(websocket)
    except PortRelaySessionError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})
        await websocket.close(code=4009, reason=str(exc))
        return

    reason = REASON_DISCONNECT
    try:
        reason = await MuxConnection(websocket, session).run()
    except asyncio.TimeoutError:
        reason = REASON_TIMEOUT
    except WebSocketDisconnect:
        reason = REASON_DISCONNECT
    finally:
        await session.detach()

    if reason in TERMINAL_REASONS:
        await session.close(reason)
        await registry.remove(session_id)


def create_port_relay_routes() -> list[Route | WebSocketRoute]:
    return [
        Route(
            "/api/port-forward/sessions",
            create_session_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/port-forward/sessions/{session_id}",
            get_session_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/port-forward/sessions/{session_id}/cancel",
            cancel_session_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/port-forward/android/pending-sessions",
            android_pending_sessions_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/port-forward/android/sessions/{session_id}/claim",
            android_claim_session_endpoint,
            methods=["POST"],
        ),
        WebSocketRoute(
            "/api/port-forward/sessions/{session_id}/phone", phone_ws_endpoint
        ),
    ]
