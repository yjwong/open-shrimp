"""Starlette routes for the security-key HID relay."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import aiosqlite
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.config import Config
from open_shrimp.db import ChatScope, get_active_context
from open_shrimp.review.auth import AuthError, authenticate
from open_shrimp.android_companion import (
    authenticate_android_request,
    create_pairing_code,
    get_or_create_server_id,
    list_active_android_push_devices,
    list_android_devices,
    pair_android_device,
    revoke_android_device,
    update_android_device_push_registration,
)
from open_shrimp.android_push import FcmPushSender, get_push_sender
from open_shrimp.security_key.db import (
    audit_security_key_event,
    create_security_key_session_record,
    get_security_key_session_record,
    list_pending_android_security_key_sessions,
    mark_security_key_session_claimed,
    mark_security_key_session_approved,
    update_security_key_session_push_status,
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


def _public_base(config: Config) -> str:
    if config.review.public_url:
        return config.review.public_url.rstrip("/")
    return f"https://{config.review.host}:{config.review.port}"


def _phone_websocket_base(config: Config) -> str:
    public_base = _public_base(config)
    if public_base.startswith("https://"):
        return "wss://" + public_base[len("https://") :]
    if public_base.startswith("http://"):
        return "ws://" + public_base[len("http://") :]
    return public_base


def _phone_url(config: Config, session: SecurityKeyRelaySession) -> str:
    return (
        f"{_phone_websocket_base(config)}/api/security-key/sessions/{session.id}/phone"
        f"?token={session.phone_token}"
    )


def _is_displayable_host(host: str | None) -> bool:
    return bool(host and host not in {"0.0.0.0", "::", "*"})


def openshrimp_server_label(config: Config) -> str:
    if config.instance_name:
        return config.instance_name
    if config.review.public_url:
        parsed = urlparse(config.review.public_url)
        if _is_displayable_host(parsed.hostname):
            return parsed.hostname or "OpenShrimp"
    if _is_displayable_host(config.review.host):
        return config.review.host
    return "OpenShrimp"


def security_key_destination_label(
    config: Config, context_name: str, sandbox_id: str | None = None
) -> str:
    server_label = openshrimp_server_label(config)
    if sandbox_id and sandbox_id != context_name:
        return f"{server_label} desktop: {context_name} ({sandbox_id})"
    return f"{server_label} desktop: {context_name}"


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
        config=config,
        push_sender=get_push_sender(request.app.state, config),
        scope=scope,
        context_name=context_name,
        sandbox_id=sandbox_id,
        lifetime_seconds=lifetime_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )

    return JSONResponse(
        {
            **session.public_dict(),
            "destination_label": security_key_destination_label(
                config, context_name, sandbox_id
            ),
            "phone_url": _phone_url(config, session),
            "phone_token": session.phone_token,
            "vm_token": session.vm_token,
        },
        status_code=201,
    )


async def create_security_key_session(
    db: aiosqlite.Connection,
    *,
    registry: SecurityKeySessionRegistry,
    config: Config | None = None,
    push_sender: FcmPushSender | None = None,
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
    if config is not None and push_sender is not None:
        await _send_android_security_key_push(
            db,
            config=config,
            push_sender=push_sender,
            session=session,
            context_name=context_name,
        )
    return session


async def _send_android_security_key_push(
    db: aiosqlite.Connection,
    *,
    config: Config,
    push_sender: FcmPushSender,
    session: SecurityKeyRelaySession,
    context_name: str,
) -> None:
    devices = await list_active_android_push_devices(db)
    if not devices:
        await update_security_key_session_push_status(
            db,
            session_id=session.id,
            requested_device_id=None,
            push_status="no_device",
        )
        return

    device = devices[0]
    server_id = await get_or_create_server_id(db)
    server_label = openshrimp_server_label(config)
    try:
        result = await push_sender.send_security_key_request(
            device=device,
            server_id=server_id,
            session_id=session.id,
            server_label=server_label,
            context_name=context_name,
        )
    except Exception:
        logger.exception("Failed to send Android companion push")
        result_status = "failed"
    else:
        result_status = result.status
    await update_security_key_session_push_status(
        db,
        session_id=session.id,
        requested_device_id=device["device_id"],
        push_status=result_status,
    )
    await audit_security_key_event(
        db,
        session_id=session.id,
        event="push_sent" if result_status == "sent" else "push_failed",
        reason=result_status,
    )


async def get_session_endpoint(request: Request) -> JSONResponse:
    """GET /api/security-key/sessions/{session_id}."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    session_id = request.path_params["session_id"]
    db: aiosqlite.Connection = request.app.state.db
    active = await _registry(request).get(session_id)
    if active is not None:
        record = await get_security_key_session_record(db, session_id=session_id)
        metadata = (
            {
                "requested_device_id": record["requested_device_id"],
                "claimed_device_id": record["claimed_device_id"],
                "push_sent_at": record["push_sent_at"],
                "push_status": record["push_status"],
                "destination_label": security_key_destination_label(
                    request.app.state.config,
                    record["context_name"],
                    record["sandbox_id"],
                ),
            }
            if record is not None
            else {}
        )
        return JSONResponse({**active.public_dict(), **metadata})

    record = await get_security_key_session_record(db, session_id=session_id)
    if record is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(
        {
            **record,
            "destination_label": security_key_destination_label(
                request.app.state.config,
                record["context_name"],
                record["sandbox_id"],
            ),
        }
    )


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


async def create_pairing_code_endpoint(request: Request) -> JSONResponse:
    """POST /api/android-companion/pairing-codes."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    config: Config = request.app.state.config
    db: aiosqlite.Connection = request.app.state.db
    pairing = await create_pairing_code(db)
    server_id = await get_or_create_server_id(db)
    return JSONResponse(
        {
            **pairing,
            "server_id": server_id,
            "server_label": openshrimp_server_label(config),
            "base_url": _public_base(config),
            "pairing_url": f"openshrimp://pair?base_url={_public_base(config)}&code={pairing['code']}",
        },
        status_code=201,
    )


async def pair_android_endpoint(request: Request) -> JSONResponse:
    """POST /api/android-companion/pair."""
    try:
        body = await _json_body(request)
        result = await pair_android_device(
            request.app.state.db,
            code=str(body.get("code", "")),
            device_id=str(body.get("device_id", "")),
            display_name=str(body.get("display_name", "")),
            public_key=str(body.get("public_key", "")),
            push_provider=(
                body.get("push_provider") if isinstance(body.get("push_provider"), str) else None
            ),
            push_token=(body.get("push_token") if isinstance(body.get("push_token"), str) else None),
            push_endpoint=(
                body.get("push_endpoint") if isinstance(body.get("push_endpoint"), str) else None
            ),
            push_auth_secret=(
                body.get("push_auth_secret")
                if isinstance(body.get("push_auth_secret"), str)
                else None
            ),
            push_p256dh=(body.get("push_p256dh") if isinstance(body.get("push_p256dh"), str) else None),
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    return JSONResponse(result, status_code=201)


async def list_android_devices_endpoint(request: Request) -> JSONResponse:
    """GET /api/android-companion/devices."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    return JSONResponse({"devices": await list_android_devices(request.app.state.db)})


async def delete_android_device_endpoint(request: Request) -> JSONResponse:
    """DELETE /api/android-companion/devices/{device_id}."""
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    ok = await revoke_android_device(
        request.app.state.db, request.path_params["device_id"]
    )
    if not ok:
        return JSONResponse({"error": "Device not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def update_android_push_registration_endpoint(request: Request) -> JSONResponse:
    """POST /api/android-companion/push-registration."""
    try:
        device = await authenticate_android_request(request)
        body = await _json_body(request)
        push_provider = body.get("push_provider")
        push_token = body.get("push_token")
        if push_provider is not None and not isinstance(push_provider, str):
            raise AuthError(400, "push_provider must be a string")
        if push_token is not None and not isinstance(push_token, str):
            raise AuthError(400, "push_token must be a string")
        await update_android_device_push_registration(
            request.app.state.db,
            device_id=device["device_id"],
            push_provider=push_provider,
            push_token=push_token,
            push_endpoint=(
                body.get("push_endpoint") if isinstance(body.get("push_endpoint"), str) else None
            ),
            push_auth_secret=(
                body.get("push_auth_secret")
                if isinstance(body.get("push_auth_secret"), str)
                else None
            ),
            push_p256dh=(body.get("push_p256dh") if isinstance(body.get("push_p256dh"), str) else None),
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)
    return JSONResponse({"ok": True})


async def android_pending_sessions_endpoint(request: Request) -> JSONResponse:
    """GET /api/security-key/android/pending-sessions."""
    try:
        device = await authenticate_android_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    sessions = await list_pending_android_security_key_sessions(request.app.state.db)
    config: Config = request.app.state.config
    public_sessions = [
        {
            "id": session["id"],
            "context_name": session["context_name"],
            "sandbox_id": session["sandbox_id"],
            "destination_label": security_key_destination_label(
                config, session["context_name"], session["sandbox_id"]
            ),
            "status": session["status"],
            "created_at": session["created_at"],
            "expires_at": session["expires_at"],
            "claimed_by_this_device": session["claimed_device_id"] == device["device_id"],
        }
        for session in sessions
        if session["claimed_device_id"] in (None, device["device_id"])
    ]
    return JSONResponse({"sessions": public_sessions})


async def android_claim_session_endpoint(request: Request) -> JSONResponse:
    """POST /api/security-key/android/sessions/{session_id}/claim."""
    try:
        device = await authenticate_android_request(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    session_id = request.path_params["session_id"]
    db: aiosqlite.Connection = request.app.state.db
    record = await get_security_key_session_record(db, session_id=session_id)
    if record is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if record["expires_at"] <= int(time.time()):
        return JSONResponse({"error": "Session expired"}, status_code=410)
    if record["ended_at"] is not None:
        return JSONResponse({"error": "Session ended"}, status_code=409)
    claimed_device_id = record.get("claimed_device_id")
    if claimed_device_id not in (None, device["device_id"]):
        return JSONResponse({"error": "Session already claimed"}, status_code=409)

    session = await _registry(request).get(session_id)
    if session is None:
        return JSONResponse({"error": "Session is no longer active"}, status_code=404)
    await mark_security_key_session_claimed(
        db, session_id=session_id, device_id=device["device_id"]
    )
    await audit_security_key_event(
        db, session_id=session_id, event="claimed", role="phone"
    )
    return JSONResponse(
        {
            "session": session.public_dict(),
            "destination_label": security_key_destination_label(
                request.app.state.config, record["context_name"], record["sandbox_id"]
            ),
            "phone_url": _phone_url(request.app.state.config, session),
        }
    )


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
        Route(
            "/api/android-companion/pairing-codes",
            create_pairing_code_endpoint,
            methods=["POST"],
        ),
        Route("/api/android-companion/pair", pair_android_endpoint, methods=["POST"]),
        Route(
            "/api/android-companion/devices",
            list_android_devices_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/android-companion/devices/{device_id}",
            delete_android_device_endpoint,
            methods=["DELETE"],
        ),
        Route(
            "/api/android-companion/push-registration",
            update_android_push_registration_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/security-key/android/pending-sessions",
            android_pending_sessions_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/security-key/android/sessions/{session_id}/claim",
            android_claim_session_endpoint,
            methods=["POST"],
        ),
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
