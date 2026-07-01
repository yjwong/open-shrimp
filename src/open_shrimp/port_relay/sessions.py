"""In-memory registry of phone port-forward relay sessions.

Unlike the security-key relay (a two-peer phone<->vm bridge), the origin
side here is in-process: the bot opens loopback TCP connections to
``127.0.0.1:<host_port>``, which an existing ``port_forward`` ssh tunnel
already maps to the sandbox guest port.  So a session tracks only the
single phone peer plus the target host port and lifecycle.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from starlette.websockets import WebSocket


MAX_SESSIONS = 20


class PortRelaySessionError(RuntimeError):
    """Raised when a relay session cannot accept an operation."""


@dataclass
class PortRelaySession:
    id: str
    chat_id: int
    thread_id: int | None
    context_name: str
    host_port: int
    label: str
    phone_token: str
    created_at: int
    expires_at: int
    idle_timeout_seconds: int
    status: str = "created"
    claimed_device_id: str | None = None
    ended_at: int | None = None
    end_reason: str | None = None
    _phone_ws: WebSocket | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def phone_connected(self) -> bool:
        return self._phone_ws is not None

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "context_name": self.context_name,
            "host_port": self.host_port,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "phone_connected": self.phone_connected,
            "claimed_device_id": self.claimed_device_id,
        }

    def validate_token(self, token: str) -> bool:
        return secrets.compare_digest(self.phone_token, token)

    def remaining_seconds(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    def is_active(self) -> bool:
        return self.ended_at is None and self.remaining_seconds() > 0

    async def attach(self, websocket: WebSocket) -> None:
        async with self._lock:
            if not self.is_active():
                raise PortRelaySessionError("session is closed")
            if self._phone_ws is not None:
                raise PortRelaySessionError("phone already connected")
            self._phone_ws = websocket
            self.status = "active"

    async def detach(self) -> None:
        async with self._lock:
            self._phone_ws = None

    async def claim(self, device_id: str) -> None:
        async with self._lock:
            if self.claimed_device_id not in (None, device_id):
                raise PortRelaySessionError("session already claimed")
            self.claimed_device_id = device_id

    async def close(self, reason: str) -> None:
        async with self._lock:
            if self.ended_at is not None:
                return
            self.status = "ended"
            self.ended_at = int(time.time())
            self.end_reason = reason
            websocket = self._phone_ws
            self._phone_ws = None
        if websocket is not None:
            try:
                await websocket.send_json({"type": "close", "reason": reason})
                await websocket.close(code=1000, reason=reason)
            except Exception:
                pass


class PortRelaySessionRegistry:
    """Process-local registry for active port-forward relay sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, PortRelaySession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        context_name: str,
        host_port: int,
        label: str,
        lifetime_seconds: int,
        idle_timeout_seconds: int,
    ) -> PortRelaySession:
        now = int(time.time())
        session = PortRelaySession(
            id=secrets.token_urlsafe(18),
            chat_id=chat_id,
            thread_id=thread_id,
            context_name=context_name,
            host_port=host_port,
            label=label,
            phone_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + lifetime_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        async with self._lock:
            self._prune_locked()
            if len(self._sessions) >= MAX_SESSIONS:
                raise PortRelaySessionError(
                    f"too many active port-forward sessions (max {MAX_SESSIONS})"
                )
            self._sessions[session.id] = session
        return session

    def _prune_locked(self) -> None:
        for session_id in [sid for sid, s in self._sessions.items() if not s.is_active()]:
            self._sessions.pop(session_id, None)

    async def get(self, session_id: str) -> PortRelaySession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_active(self) -> list[PortRelaySession]:
        async with self._lock:
            sessions = list(self._sessions.values())
        return [s for s in sessions if s.is_active()]

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
