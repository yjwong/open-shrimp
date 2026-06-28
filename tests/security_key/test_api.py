from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
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
from open_shrimp.security_key.api import _relay_loop, security_key_destination_label
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


def test_destination_label_ignores_wildcard_bind_host() -> None:
    config = _make_config()
    config.review.host = "0.0.0.0"
    assert security_key_destination_label(config, "default") == "OpenShrimp desktop: default"


def test_destination_label_prefers_public_url_hostname() -> None:
    config = _make_config()
    config.review.host = "0.0.0.0"
    config.review.public_url = "https://shrimp.example.com"
    assert (
        security_key_destination_label(config, "default")
        == "shrimp.example.com desktop: default"
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


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _android_headers(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    device_id: str,
    method: str,
    path: str,
    body: bytes = b"",
    nonce: str = "nonce-1",
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    body_hash = _b64url(hashlib.sha256(body).digest())
    payload = "\n".join([method, path, timestamp, nonce, body_hash]).encode("utf-8")
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    return {
        "X-OpenShrimp-Device-Id": device_id,
        "X-OpenShrimp-Timestamp": timestamp,
        "X-OpenShrimp-Nonce": nonce,
        "X-OpenShrimp-Signature": _b64url(signature),
    }


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


def test_android_pair_poll_and_claim_session(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    device_id = "android-test-device"
    try:
        code_response = client.post(
            "/api/android-companion/pairing-codes", headers=_auth_header()
        )
        assert code_response.status_code == 201

        pair_response = client.post(
            "/api/android-companion/pair",
            json={
                "code": code_response.json()["code"],
                "device_id": device_id,
                "display_name": "Pixel Test",
                "public_key": _b64url(public_key),
            },
        )
        assert pair_response.status_code == 201

        session_response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]

        pending_path = "/api/security-key/android/pending-sessions"
        pending_response = client.get(
            pending_path,
            headers=_android_headers(
                private_key,
                device_id=device_id,
                method="GET",
                path=pending_path,
                nonce="nonce-pending",
            ),
        )
        assert pending_response.status_code == 200
        assert pending_response.json()["sessions"][0]["id"] == session_id
        assert pending_response.json()["sessions"][0]["destination_label"].endswith(
            "desktop: default"
        )

        claim_path = f"/api/security-key/android/sessions/{session_id}/claim"
        claim_response = client.post(
            claim_path,
            content=b"{}",
            headers={
                "content-type": "application/json",
                **_android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=claim_path,
                    body=b"{}",
                    nonce="nonce-claim",
                ),
            },
        )
        assert claim_response.status_code == 200
        assert claim_response.json()["destination_label"].endswith("desktop: default")
        assert claim_response.json()["phone_url"].endswith(
            session_response.json()["phone_url"].split("/phone", 1)[1]
        )

        status = client.get(
            f"/api/security-key/sessions/{session_id}", headers=_auth_header()
        )
        assert status.json()["claimed_device_id"] == device_id
    finally:
        client.close()
        asyncio.run(db.close())


def test_security_key_session_sends_minimal_android_push(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    device_id = "android-push-device"
    sender = _FakePushSender()
    client.app.state.android_push_sender = sender
    try:
        code_response = client.post(
            "/api/android-companion/pairing-codes", headers=_auth_header()
        )
        pair_response = client.post(
            "/api/android-companion/pair",
            json={
                "code": code_response.json()["code"],
                "device_id": device_id,
                "display_name": "Pixel Push",
                "public_key": _b64url(public_key),
                "push_provider": "fcm",
                "push_token": "fcm-token",
            },
        )
        assert pair_response.status_code == 201

        session_response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]
        assert sender.sent[0]["device"]["device_id"] == device_id
        assert sender.sent[0]["device"]["push_token"] == "fcm-token"
        assert sender.sent[0]["server_id"] == pair_response.json()["server_id"]
        assert sender.sent[0]["session_id"] == session_id
        assert "phone_url" not in sender.sent[0]
        assert "phone_token" not in sender.sent[0]
        assert "vm_token" not in sender.sent[0]

        status = client.get(
            f"/api/security-key/sessions/{session_id}", headers=_auth_header()
        )
        assert status.json()["requested_device_id"] == device_id
        assert status.json()["push_status"] == "sent"
    finally:
        client.close()
        asyncio.run(db.close())


def test_android_signed_push_registration_update(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    device_id = "android-refresh-device"
    sender = _FakePushSender()
    client.app.state.android_push_sender = sender
    try:
        code_response = client.post(
            "/api/android-companion/pairing-codes", headers=_auth_header()
        )
        client.post(
            "/api/android-companion/pair",
            json={
                "code": code_response.json()["code"],
                "device_id": device_id,
                "display_name": "Pixel Refresh",
                "public_key": _b64url(public_key),
            },
        )

        path = "/api/android-companion/push-registration"
        body = b'{"push_provider":"fcm","push_token":"rotated-token"}'
        update_response = client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                **_android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=path,
                    body=body,
                    nonce="nonce-push-update",
                ),
            },
        )
        assert update_response.status_code == 200

        session_response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert session_response.status_code == 201
        assert sender.sent[0]["device"]["push_token"] == "rotated-token"
    finally:
        client.close()
        asyncio.run(db.close())


def test_security_key_session_records_no_push_device(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/security-key/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert response.status_code == 201
        status = client.get(
            f"/api/security-key/sessions/{response.json()['id']}", headers=_auth_header()
        )
        assert status.json()["requested_device_id"] is None
        assert status.json()["push_status"] == "no_device"
    finally:
        client.close()
        asyncio.run(db.close())


def test_android_signed_request_rejects_nonce_replay(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    device_id = "android-test-device"
    try:
        code_response = client.post(
            "/api/android-companion/pairing-codes", headers=_auth_header()
        )
        client.post(
            "/api/android-companion/pair",
            json={
                "code": code_response.json()["code"],
                "device_id": device_id,
                "display_name": "Pixel Test",
                "public_key": _b64url(public_key),
            },
        )

        path = "/api/security-key/android/pending-sessions"
        headers = _android_headers(
            private_key,
            device_id=device_id,
            method="GET",
            path=path,
            nonce="replayed-nonce",
        )
        assert client.get(path, headers=headers).status_code == 200
        replay = client.get(path, headers=headers)
        assert replay.status_code == 401
        assert "nonce" in replay.json()["error"]
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
        assert "desktop: default" in data["destination_label"]
        assert data["push_status"] == "no_device"
        assert data["manual_fallback"]["phone_url"] == data["phone_url"]
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


class _FakePushResult:
    status = "sent"


class _FakePushSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_security_key_request(self, **kwargs: object) -> _FakePushResult:
        self.sent.append(kwargs)
        return _FakePushResult()


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
