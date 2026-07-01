from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.db import init_db
from open_shrimp.port_relay.api import MuxConnection, port_forward_label
from open_shrimp.port_relay.frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    decode_frame,
    encode_frame,
)
from open_shrimp.review.api import create_review_app

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USER_ID = 111222333


def _make_config() -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp/test-repo",
                description="Test context",
                model="claude-sonnet-4-6",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


# --- Frame codec ----------------------------------------------------------


def test_frame_roundtrip() -> None:
    encoded = encode_frame(FRAME_DATA, 0x01020304, b"hello")
    assert decode_frame(encoded) == (FRAME_DATA, 0x01020304, b"hello")


def test_frame_open_has_empty_payload() -> None:
    assert decode_frame(encode_frame(FRAME_OPEN, 7)) == (FRAME_OPEN, 7, b"")


def test_decode_rejects_short_frame() -> None:
    with pytest.raises(ValueError):
        decode_frame(b"\x11\x00\x00")


def test_label_uses_server_and_port() -> None:
    assert port_forward_label(_make_config(), "default", 3000) == (
        "127.0.0.1 default :3000"
    )


# --- Mux loop -------------------------------------------------------------


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

    async def next_bytes(self) -> bytes:
        kind, data = await asyncio.wait_for(self.outgoing.get(), timeout=2)
        assert kind == "bytes"
        assert isinstance(data, bytes)
        return data

    async def next_frame(self) -> tuple[int, int, bytes]:
        return decode_frame(await self.next_bytes())

    async def expect_ready(self) -> None:
        kind, data = await asyncio.wait_for(self.outgoing.get(), timeout=2)
        assert kind == "json"
        assert isinstance(data, dict) and data["type"] == "ready"


async def _start_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


class _StubSession:
    def __init__(self, host_port: int) -> None:
        self.host_port = host_port
        self.idle_timeout_seconds = 30

    def remaining_seconds(self) -> int:
        return 30


@pytest.mark.asyncio
async def test_mux_echo_roundtrip() -> None:
    server, port = await _start_echo_server()
    ws = _FakeWebSocket()
    conn = MuxConnection(ws, _StubSession(port))
    run_task = asyncio.create_task(conn.run())
    try:
        await ws.expect_ready()
        await ws.incoming.put({"type": "websocket.receive", "bytes": encode_frame(FRAME_OPEN, 1)})
        await ws.incoming.put(
            {"type": "websocket.receive", "bytes": encode_frame(FRAME_DATA, 1, b"ping")}
        )
        assert await ws.next_frame() == (FRAME_DATA, 1, b"ping")

        await ws.incoming.put({"type": "websocket.disconnect"})
        assert await run_task == "disconnect"
    finally:
        if not run_task.done():
            run_task.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mux_streams_are_isolated() -> None:
    server, port = await _start_echo_server()
    ws = _FakeWebSocket()
    conn = MuxConnection(ws, _StubSession(port))
    run_task = asyncio.create_task(conn.run())
    try:
        await ws.expect_ready()
        for stream_id, payload in ((1, b"aaa"), (2, b"bbb")):
            await ws.incoming.put(
                {"type": "websocket.receive", "bytes": encode_frame(FRAME_OPEN, stream_id)}
            )
            await ws.incoming.put(
                {
                    "type": "websocket.receive",
                    "bytes": encode_frame(FRAME_DATA, stream_id, payload),
                }
            )

        received: dict[int, bytes] = {}
        for _ in range(2):
            frame_type, stream_id, payload = await ws.next_frame()
            assert frame_type == FRAME_DATA
            received[stream_id] = payload
        assert received == {1: b"aaa", 2: b"bbb"}

        await ws.incoming.put({"type": "websocket.disconnect"})
        assert await run_task == "disconnect"
    finally:
        if not run_task.done():
            run_task.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mux_notifies_phone_on_origin_eof() -> None:
    # An origin that closes immediately should produce a CLOSE frame.
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ws = _FakeWebSocket()
    conn = MuxConnection(ws, _StubSession(port))
    run_task = asyncio.create_task(conn.run())
    try:
        await ws.expect_ready()
        await ws.incoming.put({"type": "websocket.receive", "bytes": encode_frame(FRAME_OPEN, 5)})
        frame_type, stream_id, _ = await ws.next_frame()
        assert frame_type == FRAME_CLOSE
        assert stream_id == 5

        await ws.incoming.put({"type": "websocket.disconnect"})
        assert await run_task == "disconnect"
    finally:
        if not run_task.done():
            run_task.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mux_refused_origin_sends_close() -> None:
    # No server listening: OPEN should be answered with CLOSE, loop survives.
    ws = _FakeWebSocket()
    # Pick a port that's almost certainly closed.
    conn = MuxConnection(ws, _StubSession(1))
    run_task = asyncio.create_task(conn.run())
    try:
        await ws.expect_ready()
        await ws.incoming.put({"type": "websocket.receive", "bytes": encode_frame(FRAME_OPEN, 9)})
        frame_type, stream_id, _ = await ws.next_frame()
        assert frame_type == FRAME_CLOSE
        assert stream_id == 9

        await ws.incoming.put({"type": "websocket.disconnect"})
        assert await run_task == "disconnect"
    finally:
        if not run_task.done():
            run_task.cancel()


# --- HTTP endpoints -------------------------------------------------------


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
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
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
    nonce: str,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    body_hash = _b64url(hashlib.sha256(body).digest())
    payload = "\n".join([method, path, timestamp, nonce, body_hash]).encode()
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


def test_create_get_cancel_session(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/port-forward/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default", "host_port": 3000},
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["status"] == "created"
        assert data["host_port"] == 3000
        assert data["label"] == "127.0.0.1 default :3000"
        assert data["phone_token"]
        assert "/api/port-forward/sessions/" in data["phone_url"]
        assert data["phone_url"].endswith(f"token={data['phone_token']}")

        got = client.get(
            f"/api/port-forward/sessions/{data['id']}", headers=_auth_header()
        )
        assert got.status_code == 200
        assert got.json()["id"] == data["id"]

        cancel = client.post(
            f"/api/port-forward/sessions/{data['id']}/cancel", headers=_auth_header()
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"

        gone = client.get(
            f"/api/port-forward/sessions/{data['id']}", headers=_auth_header()
        )
        assert gone.status_code == 404
    finally:
        client.close()
        asyncio.run(db.close())


class _FakePushResult:
    status = "sent"


class _FakePushSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_port_forward_request(self, **kwargs: object) -> _FakePushResult:
        self.sent.append(kwargs)
        return _FakePushResult()


def test_create_session_reports_no_push_device(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/port-forward/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default", "host_port": 3000},
        )
        assert response.status_code == 201
        assert response.json()["push_status"] == "no_device"
    finally:
        client.close()
        asyncio.run(db.close())


def test_create_session_pushes_to_paired_device(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
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
                "device_id": "android-pf-push",
                "display_name": "Pixel PF Push",
                "public_key": _b64url(public_key),
                "push_provider": "fcm",
                "push_token": "fcm-token",
            },
        )

        response = client.post(
            "/api/port-forward/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default", "host_port": 8080},
        )
        assert response.status_code == 201
        assert response.json()["push_status"] == "sent"
        assert sender.sent[0]["session_id"] == response.json()["id"]
        assert sender.sent[0]["host_port"] == 8080
        assert "phone_url" not in sender.sent[0]
        assert "phone_token" not in sender.sent[0]
    finally:
        client.close()
        asyncio.run(db.close())


def test_create_session_requires_host_port(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/port-forward/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default"},
        )
        assert response.status_code == 400
        assert "host_port" in response.json()["error"]
    finally:
        client.close()
        asyncio.run(db.close())


def test_android_poll_and_claim(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    device_id = "android-pf-device"
    try:
        code_response = client.post(
            "/api/android-companion/pairing-codes", headers=_auth_header()
        )
        client.post(
            "/api/android-companion/pair",
            json={
                "code": code_response.json()["code"],
                "device_id": device_id,
                "display_name": "Pixel PF",
                "public_key": _b64url(public_key),
            },
        )

        session_response = client.post(
            "/api/port-forward/sessions",
            headers=_auth_header(),
            json={"chat_id": 123, "context_name": "default", "host_port": 5173},
        )
        session_id = session_response.json()["id"]

        pending_path = "/api/port-forward/android/pending-sessions"
        pending = client.get(
            pending_path,
            headers=_android_headers(
                private_key,
                device_id=device_id,
                method="GET",
                path=pending_path,
                nonce="nonce-pending",
            ),
        )
        assert pending.status_code == 200
        sessions = pending.json()["sessions"]
        assert sessions[0]["id"] == session_id
        assert sessions[0]["host_port"] == 5173
        assert sessions[0]["claimed_by_this_device"] is False

        claim_path = f"/api/port-forward/android/sessions/{session_id}/claim"
        claim = client.post(
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
        assert claim.status_code == 200
        assert claim.json()["label"] == "127.0.0.1 default :5173"
        assert "/phone?token=" in claim.json()["phone_url"]
    finally:
        client.close()
        asyncio.run(db.close())
