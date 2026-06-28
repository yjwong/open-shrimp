"""In-memory security-key forwarding session registry."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from starlette.websockets import WebSocket

Role = Literal["phone", "vm"]


class SecurityKeySessionError(RuntimeError):
    """Raised when a relay session cannot accept an operation."""


@dataclass
class SecurityKeyRelaySession:
    id: str
    chat_id: int
    thread_id: int | None
    context_name: str
    sandbox_id: str | None
    phone_token: str
    vm_token: str
    created_at: int
    expires_at: int
    idle_timeout_seconds: int
    status: str = "created"
    phone_connected: bool = False
    vm_connected: bool = False
    phone_approved: bool = False
    ended_at: int | None = None
    end_reason: str | None = None
    _phone_ws: WebSocket | None = field(default=None, repr=False)
    _vm_ws: WebSocket | None = field(default=None, repr=False)
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "context_name": self.context_name,
            "sandbox_id": self.sandbox_id,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "phone_connected": self.phone_connected,
            "vm_connected": self.vm_connected,
            "phone_approved": self.phone_approved,
        }

    def token_for(self, role: Role) -> str:
        return self.phone_token if role == "phone" else self.vm_token

    def validate_token(self, role: Role, token: str) -> bool:
        return secrets.compare_digest(self.token_for(role), token)

    def remaining_seconds(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    def peer(self, role: Role) -> WebSocket | None:
        return self._vm_ws if role == "phone" else self._phone_ws

    async def attach(self, role: Role, websocket: WebSocket) -> None:
        async with self._condition:
            if self.ended_at is not None or self.remaining_seconds() <= 0:
                raise SecurityKeySessionError("session is closed")
            if role == "phone":
                if self._phone_ws is not None:
                    raise SecurityKeySessionError("phone already connected")
                self._phone_ws = websocket
                self.phone_connected = True
            else:
                if self._vm_ws is not None:
                    raise SecurityKeySessionError("vm already connected")
                self._vm_ws = websocket
                self.vm_connected = True
            if self.status == "created":
                self.status = "connecting"
            if self.phone_connected and self.vm_connected:
                self.status = "active"
            self._condition.notify_all()

    async def detach(self, role: Role) -> None:
        async with self._condition:
            if role == "phone" and self._phone_ws is not None:
                self._phone_ws = None
                self.phone_connected = False
            elif role == "vm" and self._vm_ws is not None:
                self._vm_ws = None
                self.vm_connected = False
            self._condition.notify_all()

    async def wait_for_peer(self, role: Role) -> None:
        async with self._condition:
            while self.peer(role) is None and self.ended_at is None:
                remaining = self.remaining_seconds()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(self._condition.wait(), timeout=remaining)
            if self.ended_at is not None:
                raise SecurityKeySessionError("session is closed")

    async def mark_approved(self) -> None:
        async with self._condition:
            self.phone_approved = True
            if self.status in {"created", "connecting"}:
                self.status = "approved"
            self._condition.notify_all()

    async def close(self, reason: str) -> None:
        async with self._condition:
            if self.ended_at is not None:
                return
            self.status = "ended"
            self.ended_at = int(time.time())
            self.end_reason = reason
            peers = [ws for ws in (self._phone_ws, self._vm_ws) if ws is not None]
            self._phone_ws = None
            self._vm_ws = None
            self.phone_connected = False
            self.vm_connected = False
            self._condition.notify_all()
        for websocket in peers:
            try:
                await websocket.send_json({"type": "close", "reason": reason})
                await websocket.close(code=1000, reason=reason)
            except Exception:
                pass


class SecurityKeySessionRegistry:
    """Process-local registry for active forwarding sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SecurityKeyRelaySession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        context_name: str,
        sandbox_id: str | None,
        lifetime_seconds: int,
        idle_timeout_seconds: int,
    ) -> SecurityKeyRelaySession:
        now = int(time.time())
        session = SecurityKeyRelaySession(
            id=secrets.token_urlsafe(18),
            chat_id=chat_id,
            thread_id=thread_id,
            context_name=context_name,
            sandbox_id=sandbox_id,
            phone_token=secrets.token_urlsafe(32),
            vm_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + lifetime_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        async with self._lock:
            self._sessions[session.id] = session
        return session

    async def get(self, session_id: str) -> SecurityKeyRelaySession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
