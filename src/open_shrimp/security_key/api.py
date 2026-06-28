"""Starlette routes for the security-key HID relay."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiosqlite
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.config import Config
from open_shrimp.db import ChatScope, get_active_context
from open_shrimp.review.auth import AuthError, authenticate
from open_shrimp.security_key.db import (
    audit_security_key_event,
    create_security_key_session_record,
    get_security_key_session_record,
    mark_security_key_session_approved,
    update_security_key_session_status,
)
from open_shrimp.security_key.sessions import (
    Role,
    SecurityKeyRelaySession,
    SecurityKeySessionError,
    SecurityKeySessionRegistry,
)

logger = logging.getLogger(__name__)

DEFAULT_SESSION_LIFETIME_SECONDS = 300
DEFAULT_IDLE_TIMEOUT_SECONDS = 300
MAX_SESSION_LIFETIME_SECONDS = 300
MAX_IDLE_TIMEOUT_SECONDS = 300


def _registry(request_or_ws: Request | WebSocket) -> SecurityKeySessionRegistry:
    registry = getattr(request_or_ws.app.state, "security_key_registry", None)
    if registry is None:
        registry = SecurityKeySessionRegistry()
        request_or_ws.app.state.security_key_registry = registry
    return registry


def get_or_create_registry(state: Any) -> SecurityKeySessionRegistry:
    """Return the shared security-key relay registry from an app/bot state."""
    registry = getattr(state, "security_key_registry", None)
    if registry is None and isinstance(state, dict):
        registry = state.get("security_key_registry")
    if registry is None:
        registry = SecurityKeySessionRegistry()
        if isinstance(state, dict):
            state["security_key_registry"] = registry
        else:
            state.security_key_registry = registry
    return registry


async def _authenticate(request: Request) -> int:
    config: Config = request.app.state.config
    return await authenticate(
        request.headers.get("authorization", ""),
        config.telegram.token,
        config.allowed_users,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise AuthError(400, "Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise AuthError(400, "JSON body must be an object")
    return body


def _bounded_seconds(
    raw: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field: str,
) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AuthError(400, f"{field} must be an integer") from exc
    if value < minimum or value > maximum:
        raise AuthError(400, f"{field} must be between {minimum} and {maximum}")
    return value


async def _resolve_context(
    db: aiosqlite.Connection,
    config: Config,
    scope: ChatScope,
    raw_context_name: object,
) -> str:
    if raw_context_name is None:
        context_name = await get_active_context(db, scope)
        if context_name is None:
            context_name = config.default_context
    elif isinstance(raw_context_name, str) and raw_context_name:
        context_name = raw_context_name
    else:
        raise AuthError(400, "context_name must be a non-empty string")

    if context_name not in config.contexts:
        raise AuthError(404, f"Context '{context_name}' not found")
    return context_name


async def create_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/security-key/sessions."""
    try:
        await _authenticate(request)
        body = await _json_body(request)
        chat_id = int(body["chat_id"])
        raw_thread_id = body.get("thread_id")
        thread_id = int(raw_thread_id) if raw_thread_id is not None else None
        sandbox_id_raw = body.get("sandbox_id")
        sandbox_id = sandbox_id_raw if isinstance(sandbox_id_raw, str) else None
        lifetime_seconds = _bounded_seconds(
            body.get("lifetime_seconds"),
            default=DEFAULT_SESSION_LIFETIME_SECONDS,
            minimum=10,
            maximum=MAX_SESSION_LIFETIME_SECONDS,
            field="lifetime_seconds",
        )
        idle_timeout_seconds = _bounded_seconds(
            body.get("idle_timeout_seconds"),
            default=DEFAULT_IDLE_TIMEOUT_SECONDS,
            minimum=10,
            maximum=MAX_IDLE_TIMEOUT_SECONDS,
            field="idle_timeout_seconds",
        )
    except KeyError:
        return JSONResponse({"error": "chat_id is required"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "chat_id and thread_id must be integers"}, status_code=400
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    config: Config = request.app.state.config
    db: aiosqlite.Connection = request.app.state.db
    scope = ChatScope(chat_id, thread_id)
    try:
        context_name = await _resolve_context(
            db, config, scope, body.get("context_name")
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    session = await create_security_key_session(
        db,
        registry=_registry(request),
        scope=scope,
        context_name=context_name,
        sandbox_id=sandbox_id,
        lifetime_seconds=lifetime_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )

    return JSONResponse(
        {
            **session.public_dict(),
            "phone_token": session.phone_token,
            "vm_token": session.vm_token,
        },
        status_code=201,
    )


async def create_security_key_session(
    db: aiosqlite.Connection,
    *,
    registry: SecurityKeySessionRegistry,
    scope: ChatScope,
    context_name: str,
    sandbox_id: str | None,
    lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> SecurityKeyRelaySession:
    """Create an active relay session and persist audit metadata."""
    session = await registry.create(
        chat_id=scope.chat_id,
        thread_id=scope.thread_id,
        context_name=context_name,
        sandbox_id=sandbox_id,
        lifetime_seconds=lifetime_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    await create_security_key_session_record(
        db,
        session_id=session.id,
        scope=scope,
        context_name=context_name,
        sandbox_id=sandbox_id,
        expires_at=session.expires_at,
    )
    await audit_security_key_event(db, session_id=session.id, event="created")
    return session


async def get_session_endpoint(request: Request) -> JSONResponse:
    """GET /api/security-key/sessions/{session_id}."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    session_id = request.path_params["session_id"]
    active = await _registry(request).get(session_id)
    if active is not None:
        return JSONResponse(active.public_dict())

    db: aiosqlite.Connection = request.app.state.db
    record = await get_security_key_session_record(db, session_id=session_id)
    if record is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(record)


async def cancel_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/security-key/sessions/{session_id}/cancel."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    session_id = request.path_params["session_id"]
    db: aiosqlite.Connection = request.app.state.db
    session = await _registry(request).get(session_id)
    if session is None:
        record = await get_security_key_session_record(db, session_id=session_id)
        if record is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse({"ok": True, "status": record["status"]})

    await session.close("cancelled")
    await _registry(request).remove(session_id)
    await update_security_key_session_status(
        db, session_id=session_id, status="cancelled", end_reason="cancelled"
    )
    await audit_security_key_event(db, session_id=session_id, event="cancelled")
    return JSONResponse({"ok": True, "status": "cancelled"})


async def _send_peer_event(
    session: SecurityKeyRelaySession, role: Role, event: str
) -> None:
    peer = session.peer(role)
    if peer is not None:
        await peer.send_json({"type": event, "role": role})


def _valid_frame_type(role: Role, payload: bytes) -> bool:
    if not payload:
        return False
    frame_type = payload[0]
    if frame_type == 0x03:
        return True
    if role == "vm":
        return frame_type == 0x01
    return frame_type == 0x02


async def _relay_loop(
    websocket: WebSocket,
    session: SecurityKeyRelaySession,
    role: Role,
) -> str:
    await session.wait_for_peer(role)
    await websocket.send_json({"type": "ready"})
    await _send_peer_event(session, role, "peer_connected")

    while True:
        timeout = min(session.idle_timeout_seconds, max(1, session.remaining_seconds()))
        message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
        msg_type = message.get("type")
        if msg_type == "websocket.disconnect":
            return "disconnect"
        if "bytes" in message and message["bytes"] is not None:
            payload = message["bytes"]
            if not _valid_frame_type(role, payload):
                await websocket.send_json({"type": "error", "error": "invalid_frame_type"})
                continue
            peer = session.peer(role)
            if peer is None:
                return "peer_disconnected"
            await peer.send_bytes(payload)
            continue
        if "text" in message and message["text"] is not None:
            try:
                control = json.loads(message["text"])
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "invalid_json"})
                continue
            if not isinstance(control, dict):
                await websocket.send_json({"type": "error", "error": "invalid_control"})
                continue
            control_type = control.get("type")
            if role == "phone" and control_type == "approved":
                await session.mark_approved()
                await mark_security_key_session_approved(
                    websocket.app.state.db,
                    session_id=session.id,
                    device_id=(
                        control.get("device_id")
                        if isinstance(control.get("device_id"), str)
                        else None
                    ),
                )
                await audit_security_key_event(
                    websocket.app.state.db,
                    session_id=session.id,
                    event="approved",
                    role=role,
                )
            elif control_type == "cancel":
                return "cancelled"
            peer = session.peer(role)
            if peer is not None:
                await peer.send_json(control)


async def _websocket_endpoint(websocket: WebSocket, role: Role) -> None:
    session_id = websocket.path_params["session_id"]
    token = websocket.query_params.get("token", "")
    registry = _registry(websocket)
    db: aiosqlite.Connection = websocket.app.state.db

    session = await registry.get(session_id)
    if session is None:
        await websocket.close(code=4004, reason="Session not found")
        return
    if not token or not session.validate_token(role, token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    try:
        await session.attach(role, websocket)
    except SecurityKeySessionError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})
        await websocket.close(code=4009, reason=str(exc))
        return

    await update_security_key_session_status(
        db, session_id=session_id, status=session.status
    )
    await audit_security_key_event(
        db, session_id=session_id, event="connected", role=role
    )
    await websocket.send_json({"type": "hello", "version": 1, "role": role})

    reason = "disconnect"
    try:
        reason = await _relay_loop(websocket, session, role)
    except asyncio.TimeoutError:
        reason = "timeout"
    except WebSocketDisconnect:
        reason = "disconnect"
    except SecurityKeySessionError:
        reason = "closed"
    finally:
        await session.detach(role)
        await audit_security_key_event(
            db,
            session_id=session_id,
            event="disconnected",
            role=role,
            reason=reason,
        )

    if reason in {"disconnect", "timeout", "cancelled", "peer_disconnected"}:
        await session.close(reason)
        await registry.remove(session_id)
        await update_security_key_session_status(
            db, session_id=session_id, status="ended", end_reason=reason
        )
        await audit_security_key_event(
            db, session_id=session_id, event=reason, role=role
        )


async def phone_ws_endpoint(websocket: WebSocket) -> None:
    await _websocket_endpoint(websocket, "phone")


async def vm_ws_endpoint(websocket: WebSocket) -> None:
    await _websocket_endpoint(websocket, "vm")


def create_security_key_routes() -> list[Route | WebSocketRoute]:
    return [
        Route("/api/security-key/sessions", create_session_endpoint, methods=["POST"]),
        Route(
            "/api/security-key/sessions/{session_id}",
            get_session_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/security-key/sessions/{session_id}/cancel",
            cancel_session_endpoint,
            methods=["POST"],
        ),
        WebSocketRoute(
            "/api/security-key/sessions/{session_id}/phone", phone_ws_endpoint
        ),
        WebSocketRoute("/api/security-key/sessions/{session_id}/vm", vm_ws_endpoint),
    ]
