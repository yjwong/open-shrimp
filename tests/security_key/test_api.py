from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from starlette.testclient import TestClient

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    SandboxConfig,
    TelegramConfig,
)
from open_shrimp.db import init_db
from open_shrimp.review.api import create_review_app
from open_shrimp.security_key.api import _relay_loop
from open_shrimp.security_key.sessions import SecurityKeySessionRegistry

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USER_ID = 111222333


def _make_config(*, computer_use: bool = False) -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp/test-repo",
                description="Test context",
                model="claude-sonnet-4-6",
                allowed_tools=[],
                sandbox=(
                    SandboxConfig(backend="docker", computer_use=True)
                    if computer_use
                    else None
                ),
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


def _build_init_data() -> str:
    user_obj = json.dumps(
        {"id": ALLOWED_USER_ID, "first_name": "Test"}, separators=(",", ":")
    )
    params = {
        "auth_date": str(int(time.time())),
        "user": user_obj,
        "query_id": "AAHQ",
    }
    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    params["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def _auth_header() -> dict[str, str]:
    return {"authorization": f"tg-init-data {_build_init_data()}"}


def _make_client(tmp_path: Path) -> tuple[TestClient, object]:
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    app = create_review_app(_make_config(), db)
    return TestClient(app), db


def _make_computer_use_client(
    tmp_path: Path,
    *,
    sandbox: object | None = None,
) -> tuple[TestClient, object]:
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    registry = SecurityKeySessionRegistry()
    app = create_review_app(
        _make_config(computer_use=True),
        db,
        sandbox_managers={"docker": _FakeSandboxManager(sandbox)} if sandbox else None,
        security_key_registry=registry,
    )
    return TestClient(app), db


def test_create_session_persists_metadata(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "created"
        assert data["context_name"] == "default"
        assert data["phone_token"]
        assert data["vm_token"]

        status = client.get(
            f"/api/security-key/sessions/{data['id']}", headers=_auth_header()
        )
        assert status.status_code == 200
        assert status.json()["id"] == data["id"]
    finally:
        client.close()
        asyncio.run(db.close())


@pytest.mark.asyncio
async def test_relay_loop_exchanges_binary_hid_frames() -> None:
    registry = SecurityKeySessionRegistry()
    session = await registry.create(
        chat_id=123,
        thread_id=None,
        context_name="default",
        sandbox_id=None,
        lifetime_seconds=60,
        idle_timeout_seconds=10,
    )
    phone = _FakeWebSocket()
    vm = _FakeWebSocket()
    await session.attach("phone", phone)
    await session.attach("vm", vm)

    phone_task = asyncio.create_task(_relay_loop(phone, session, "phone"))
    vm_task = asyncio.create_task(_relay_loop(vm, session, "vm"))
    try:
        assert await phone.next_json_type("ready") == "ready"
        assert await vm.next_json_type("ready") == "ready"

        await vm.incoming.put({"type": "websocket.receive", "bytes": b"\x01abc"})
        assert await phone.next_bytes() == b"\x01abc"
        await phone.incoming.put({"type": "websocket.receive", "bytes": b"\x02def"})
        assert await vm.next_bytes() == b"\x02def"

        await phone.incoming.put({"type": "websocket.disconnect"})
        await vm.incoming.put({"type": "websocket.disconnect"})
        assert await phone_task == "disconnect"
        assert await vm_task == "disconnect"
    finally:
        for task in (phone_task, vm_task):
            if not task.done():
                task.cancel()


def test_cancel_session_closes_metadata(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        session_id = response.json()["id"]

        cancel = client.post(
            f"/api/security-key/sessions/{session_id}/cancel", headers=_auth_header()
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"

        status = client.get(
            f"/api/security-key/sessions/{session_id}", headers=_auth_header()
        )
        assert status.status_code == 200
        assert status.json()["end_reason"] == "cancelled"
    finally:
        client.close()
        asyncio.run(db.close())


def test_vnc_endpoint_creates_security_key_session(tmp_path: Path) -> None:
    sandbox = _FakeSandbox()
    client, db = _make_computer_use_client(tmp_path, sandbox=sandbox)
    try:
        response = client.post(
            "/api/vnc/security-key-session"
            f"?context=default&token={quote(_build_init_data())}",
            json={"chat_id": 123},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "created"
        assert "/api/security-key/sessions/" in data["phone_url"]
        assert data["vm_helper_command"].startswith(
            "sudo openshrimp-security-key-vm-helper "
        )
        assert data["vm_helper_started"] is True
        assert data["vm_helper_error"] is None
        assert len(sandbox.started) == 1
        started = sandbox.started[0]
        assert started["relay_url"] == "ws://host.docker.internal:8080"
        assert started["session_id"] == data["id"]
        assert started["token"]

        status = client.get(
            f"/api/security-key/sessions/{data['id']}", headers=_auth_header()
        )
        assert status.status_code == 200
        assert status.json()["id"] == data["id"]
    finally:
        client.close()
        asyncio.run(db.close())


def test_vnc_endpoint_reports_security_key_helper_start_failure(
    tmp_path: Path,
) -> None:
    client, db = _make_computer_use_client(
        tmp_path,
        sandbox=_FakeSandbox(start_error=RuntimeError("sudo unavailable")),
    )
    try:
        response = client.post(
            "/api/vnc/security-key-session"
            f"?context=default&token={quote(_build_init_data())}",
            json={"chat_id": 123},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["vm_helper_started"] is False
        assert data["vm_helper_error"] == "sudo unavailable"
    finally:
        client.close()
        asyncio.run(db.close())


class _FakeSandboxManager:
    def __init__(self, sandbox: object | None) -> None:
        self.sandbox = sandbox

    def create_sandbox(self, context_name: str, ctx: object) -> object | None:
        return self.sandbox


class _FakeSandbox:
    host_address = "host.docker.internal"
    container_name = "openshrimp-test"

    def __init__(self, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.started: list[dict[str, str]] = []

    def start_security_key_helper(
        self,
        *,
        relay_url: str,
        session_id: str,
        token: str,
    ) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(
            {"relay_url": relay_url, "session_id": session_id, "token": token}
        )


class _FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    async def receive(self) -> dict[str, object]:
        return await self.incoming.get()

    async def send_json(self, data: object) -> None:
        await self.outgoing.put(("json", data))

    async def send_bytes(self, data: bytes) -> None:
        await self.outgoing.put(("bytes", data))

    async def next_json_type(self, expected: str | None = None) -> str:
        for _ in range(5):
            kind, data = await asyncio.wait_for(self.outgoing.get(), timeout=1)
            assert kind == "json"
            assert isinstance(data, dict)
            message_type = str(data["type"])
            if expected is None or message_type == expected:
                return message_type
        raise AssertionError(f"did not receive {expected}")

    async def next_bytes(self) -> bytes:
        for _ in range(5):
            kind, data = await asyncio.wait_for(self.outgoing.get(), timeout=1)
            if kind != "bytes":
                continue
            assert isinstance(data, bytes)
            return data
        raise AssertionError("did not receive bytes")
