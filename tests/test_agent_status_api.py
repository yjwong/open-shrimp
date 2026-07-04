from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.db import init_db
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


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


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


def _pair(client: TestClient, private_key: ec.EllipticCurvePrivateKey, device_id: str) -> None:
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    code = client.post(
        "/api/android-companion/pairing-codes", headers=_auth_header()
    ).json()["code"]
    client.post(
        "/api/android-companion/pair",
        json={
            "code": code,
            "device_id": device_id,
            "display_name": "Pixel",
            "public_key": _b64url(public_key),
        },
    )


def _make_client(tmp_path: Path) -> tuple[TestClient, object]:
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    app = create_review_app(_make_config(), db)
    return TestClient(app), db


class _FakeFuture:
    """Minimal stand-in for asyncio.Future.

    The endpoint only touches ``done()`` and ``set_result()``; using a fake
    sidesteps the cross-event-loop issue where TestClient runs the app in its
    own loop (in production the HTTP server and bot share one loop).
    """

    def __init__(self) -> None:
        self.result_value: bool | None = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: bool) -> None:
        self.result_value = value
        self._done = True


def test_android_approval_resolves_pending_future(tmp_path: Path) -> None:
    from open_shrimp.handlers.state import _approval_futures, _approval_resolved_via

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-approve-device"
    tool_use_id = "tool-abc"
    future = _FakeFuture()
    _approval_futures[f"approve:{tool_use_id}"] = future  # type: ignore[assignment]
    _approval_futures[f"deny:{tool_use_id}"] = future  # type: ignore[assignment]
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"approve"}'
        resp = client.post(
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
                    nonce="nonce-approve",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "decision": "approve"}
        assert future.result_value is True
        assert _approval_resolved_via.get(tool_use_id) == "android"
    finally:
        _approval_futures.pop(f"approve:{tool_use_id}", None)
        _approval_futures.pop(f"deny:{tool_use_id}", None)
        _approval_resolved_via.pop(tool_use_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_approval_resolves_host_escape_future(tmp_path: Path) -> None:
    from open_shrimp.handlers.state import _approval_futures, _approval_resolved_via

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-hostbash-device"
    tool_use_id = "tool-hb"
    future = _FakeFuture()
    # Host-escape prompts register under the ``hb_approve:``/``hb_deny:`` keys.
    _approval_futures[f"hb_approve:{tool_use_id}"] = future  # type: ignore[assignment]
    _approval_futures[f"hb_deny:{tool_use_id}"] = future  # type: ignore[assignment]
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"approve"}'
        resp = client.post(
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
                    nonce="nonce-hostbash",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "decision": "approve"}
        assert future.result_value is True
        # The host-escape flow edits its own message, so no phone marker is set.
        assert tool_use_id not in _approval_resolved_via
    finally:
        _approval_futures.pop(f"hb_approve:{tool_use_id}", None)
        _approval_futures.pop(f"hb_deny:{tool_use_id}", None)
        _approval_resolved_via.pop(tool_use_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_approval_noops_when_future_missing(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-expired-device"
    tool_use_id = "tool-gone"
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"deny"}'
        resp = client.post(
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
                    nonce="nonce-expired",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "expired"}
    finally:
        client.close()
        asyncio.run(db.close())


def test_android_approval_rejects_unsigned_request(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        resp = client.post(
            "/api/agent/approvals/tool-x",
            json={"decision": "approve"},
        )
        assert resp.status_code == 401
    finally:
        client.close()
        asyncio.run(db.close())
