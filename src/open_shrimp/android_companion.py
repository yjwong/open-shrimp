"""Android companion pairing and signed-request helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from typing import Any

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from starlette.requests import Request

from open_shrimp.review.auth import AuthError

PAIRING_CODE_TTL_SECONDS = 600
SIGNED_REQUEST_MAX_AGE_SECONDS = 300
NONCE_RETENTION_SECONDS = 900


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


async def get_or_create_server_id(db: aiosqlite.Connection) -> str:
    cursor = await db.execute("SELECT server_id FROM android_companion_instance WHERE id = 1")
    row = await cursor.fetchone()
    if row is not None:
        return row[0]
    server_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO android_companion_instance (id, server_id, created_at) VALUES (1, ?, ?)",
        (server_id, int(time.time())),
    )
    await db.commit()
    return server_id


async def create_pairing_code(db: aiosqlite.Connection) -> dict[str, Any]:
    code = secrets.token_urlsafe(12)
    now = int(time.time())
    expires_at = now + PAIRING_CODE_TTL_SECONDS
    await db.execute(
        "INSERT INTO android_companion_pairing_codes (code, expires_at, created_at) VALUES (?, ?, ?)",
        (code, expires_at, now),
    )
    await db.commit()
    return {"code": code, "expires_at": expires_at}


async def list_android_devices(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """
        SELECT device_id, display_name, active, push_provider, created_at, last_seen_at, revoked_at
        FROM android_companion_devices
        ORDER BY created_at DESC
        """
    )
    rows = await cursor.fetchall()
    return [
        {
            "device_id": row[0],
            "display_name": row[1],
            "active": bool(row[2]),
            "push_provider": row[3],
            "created_at": row[4],
            "last_seen_at": row[5],
            "revoked_at": row[6],
        }
        for row in rows
    ]


async def list_active_android_push_devices(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """
        SELECT device_id, display_name, push_provider, push_token,
               push_endpoint, push_auth_secret, push_p256dh
        FROM android_companion_devices
        WHERE active = 1
          AND revoked_at IS NULL
          AND push_provider IS NOT NULL
        ORDER BY created_at DESC
        """
    )
    rows = await cursor.fetchall()
    return [
        {
            "device_id": row[0],
            "display_name": row[1],
            "push_provider": row[2],
            "push_token": row[3],
            "push_endpoint": row[4],
            "push_auth_secret": row[5],
            "push_p256dh": row[6],
        }
        for row in rows
    ]


async def revoke_android_device(db: aiosqlite.Connection, device_id: str) -> bool:
    cursor = await db.execute(
        """
        UPDATE android_companion_devices
        SET active = 0, revoked_at = COALESCE(revoked_at, ?)
        WHERE device_id = ? AND revoked_at IS NULL
        """,
        (int(time.time()), device_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_android_device_push_registration(
    db: aiosqlite.Connection,
    *,
    device_id: str,
    push_provider: str | None,
    push_token: str | None,
    push_endpoint: str | None = None,
    push_auth_secret: str | None = None,
    push_p256dh: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE android_companion_devices
        SET push_provider = ?, push_token = ?, push_endpoint = ?,
            push_auth_secret = ?, push_p256dh = ?, last_seen_at = ?
        WHERE device_id = ? AND active = 1 AND revoked_at IS NULL
        """,
        (
            push_provider,
            push_token,
            push_endpoint,
            push_auth_secret,
            push_p256dh,
            int(time.time()),
            device_id,
        ),
    )
    await db.commit()


async def pair_android_device(
    db: aiosqlite.Connection,
    *,
    code: str,
    device_id: str,
    display_name: str,
    public_key: str,
    push_provider: str | None = None,
    push_token: str | None = None,
    push_endpoint: str | None = None,
    push_auth_secret: str | None = None,
    push_p256dh: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    cursor = await db.execute(
        "SELECT expires_at, used_at FROM android_companion_pairing_codes WHERE code = ?",
        (code,),
    )
    row = await cursor.fetchone()
    if row is None or row[1] is not None or row[0] < now:
        raise AuthError(400, "Invalid or expired pairing code")

    if not device_id or not display_name or not public_key:
        raise AuthError(400, "device_id, display_name, and public_key are required")

    await db.execute("UPDATE android_companion_devices SET active = 0 WHERE active = 1")
    await db.execute(
        "UPDATE android_companion_pairing_codes SET used_at = ? WHERE code = ?",
        (now, code),
    )
    await db.execute(
        """
        INSERT INTO android_companion_devices (
            device_id, display_name, public_key, active, push_provider, push_token,
            push_endpoint, push_auth_secret, push_p256dh, created_at, last_seen_at
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            display_name = excluded.display_name,
            public_key = excluded.public_key,
            active = 1,
            push_provider = excluded.push_provider,
            push_token = excluded.push_token,
            push_endpoint = excluded.push_endpoint,
            push_auth_secret = excluded.push_auth_secret,
            push_p256dh = excluded.push_p256dh,
            last_seen_at = excluded.last_seen_at,
            revoked_at = NULL
        """,
        (
            device_id,
            display_name,
            public_key,
            push_provider,
            push_token,
            push_endpoint,
            push_auth_secret,
            push_p256dh,
            now,
            now,
        ),
    )
    await db.commit()
    return {
        "server_id": await get_or_create_server_id(db),
        "device_id": device_id,
        "display_name": display_name,
    }


async def _get_active_device(db: aiosqlite.Connection, device_id: str) -> dict[str, Any] | None:
    cursor = await db.execute(
        """
        SELECT device_id, display_name, public_key
        FROM android_companion_devices
        WHERE device_id = ? AND active = 1 AND revoked_at IS NULL
        """,
        (device_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"device_id": row[0], "display_name": row[1], "public_key": row[2]}


async def authenticate_android_request(request: Request) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    device_id = request.headers.get("x-openshrimp-device-id", "")
    timestamp_raw = request.headers.get("x-openshrimp-timestamp", "")
    nonce = request.headers.get("x-openshrimp-nonce", "")
    signature_raw = request.headers.get("x-openshrimp-signature", "")
    if not device_id or not timestamp_raw or not nonce or not signature_raw:
        raise AuthError(401, "Missing Android companion signature headers")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise AuthError(401, "Invalid Android companion timestamp") from exc
    now = int(time.time())
    if abs(now - timestamp) > SIGNED_REQUEST_MAX_AGE_SECONDS:
        raise AuthError(401, "Android companion signature timestamp is stale")

    device = await _get_active_device(db, device_id)
    if device is None:
        raise AuthError(401, "Android companion device is not paired")

    body = await request.body()
    body_hash = _b64url_encode(hashlib.sha256(body).digest())
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    signed_payload = "\n".join(
        [request.method.upper(), path, str(timestamp), nonce, body_hash]
    ).encode("utf-8")

    public_key_bytes = _b64url_decode(device["public_key"])
    public_key = serialization.load_der_public_key(public_key_bytes)
    signature = _b64url_decode(signature_raw)
    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, signed_payload, ec.ECDSA(hashes.SHA256()))
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, signed_payload, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise AuthError(401, "Unsupported Android companion public key")
    except InvalidSignature as exc:
        raise AuthError(401, "Invalid Android companion signature") from exc

    await db.execute(
        "DELETE FROM android_companion_nonces WHERE created_at < ?",
        (now - NONCE_RETENTION_SECONDS,),
    )
    try:
        await db.execute(
            "INSERT INTO android_companion_nonces (device_id, nonce, created_at) VALUES (?, ?, ?)",
            (device_id, nonce, now),
        )
    except aiosqlite.IntegrityError as exc:
        raise AuthError(401, "Android companion nonce was already used") from exc
    await db.execute(
        "UPDATE android_companion_devices SET last_seen_at = ? WHERE device_id = ?",
        (now, device_id),
    )
    await db.commit()
    return device
