"""Push notification delivery for paired Android companion devices."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from open_shrimp.config import Config

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass
class PushDeliveryResult:
    status: str
    message_id: str | None = None
    error: str | None = None


class FcmPushSender:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._access_token: str | None = None
        self._access_token_expires_at = 0

    def _service_account(self) -> dict[str, Any] | None:
        raw_json = (
            self._config.android_companion.fcm_service_account_json
            or os.environ.get("OPENSHRIMP_FCM_SERVICE_ACCOUNT_JSON")
        )
        if raw_json:
            return json.loads(raw_json)

        raw_path = (
            self._config.android_companion.fcm_service_account_file
            or os.environ.get("OPENSHRIMP_FCM_SERVICE_ACCOUNT_FILE")
        )
        if raw_path:
            return json.loads(Path(raw_path).read_text(encoding="utf-8"))

        return None

    def _project_id(self, service_account: dict[str, Any]) -> str | None:
        return (
            self._config.android_companion.fcm_project_id
            or os.environ.get("OPENSHRIMP_FCM_PROJECT_ID")
            or service_account.get("project_id")
        )

    async def _access_token_for(self, service_account: dict[str, Any]) -> str:
        now = int(time.time())
        if self._access_token and self._access_token_expires_at - 60 > now:
            return self._access_token

        client_email = service_account.get("client_email")
        private_key_pem = service_account.get("private_key")
        if not isinstance(client_email, str) or not isinstance(private_key_pem, str):
            raise RuntimeError("FCM service account is missing client_email or private_key")

        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": client_email,
            "scope": FCM_SCOPE,
            "aud": FCM_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        ).encode("ascii")
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = signing_input.decode("ascii") + "." + _b64url(signature)

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                FCM_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"FCM token request failed: HTTP {response.status_code}")
        body = response.json()
        access_token = body.get("access_token")
        expires_in = int(body.get("expires_in", 3600))
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("FCM token response did not include an access_token")
        self._access_token = access_token
        self._access_token_expires_at = now + expires_in
        return access_token

    async def _post_fcm_message(
        self,
        *,
        token: str,
        data: dict[str, str],
        high_priority: bool,
    ) -> PushDeliveryResult:
        """POST a single FCM data message, resolving credentials and token."""
        service_account = self._service_account()
        if service_account is None:
            return PushDeliveryResult(status="not_configured")
        project_id = self._project_id(service_account)
        if not project_id:
            return PushDeliveryResult(status="not_configured")

        access_token = await self._access_token_for(service_account)
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        payload = {
            "message": {
                "token": token,
                "data": data,
                "android": {"priority": "HIGH" if high_priority else "NORMAL"},
            }
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                headers={"authorization": f"Bearer {access_token}"},
                json=payload,
            )
        if response.status_code >= 400:
            logger.warning("FCM send failed: HTTP %s %s", response.status_code, response.text)
            return PushDeliveryResult(status="failed", error=f"HTTP {response.status_code}")
        message_id = response.json().get("name")
        return PushDeliveryResult(
            status="sent",
            message_id=message_id if isinstance(message_id, str) else None,
        )

    async def send_security_key_request(
        self,
        *,
        device: dict[str, Any],
        server_id: str,
        session_id: str,
        server_label: str,
        context_name: str,
    ) -> PushDeliveryResult:
        if device.get("push_provider") != "fcm":
            return PushDeliveryResult(status="unsupported_provider")
        token = device.get("push_token")
        if not isinstance(token, str) or not token:
            return PushDeliveryResult(status="missing_token")
        return await self._post_fcm_message(
            token=token,
            data={
                "type": "security_key_request",
                "server_id": server_id,
                "session_id": session_id,
            },
            high_priority=True,
        )

    async def send_port_forward_request(
        self,
        *,
        device: dict[str, Any],
        server_id: str,
        session_id: str,
        label: str,
        host_port: int,
    ) -> PushDeliveryResult:
        if device.get("push_provider") != "fcm":
            return PushDeliveryResult(status="unsupported_provider")
        token = device.get("push_token")
        if not isinstance(token, str) or not token:
            return PushDeliveryResult(status="missing_token")
        return await self._post_fcm_message(
            token=token,
            data={
                "type": "port_forward_request",
                "server_id": server_id,
                "session_id": session_id,
                "label": label,
                "host_port": str(host_port),
            },
            high_priority=True,
        )

    async def send_agent_status(
        self,
        *,
        device: dict[str, Any],
        data: dict[str, str],
        high_priority: bool = False,
    ) -> PushDeliveryResult:
        """Send an ``agent_status`` FCM data message to an Android device.

        ``data`` is the per-ChatScope event payload (see
        :mod:`open_shrimp.agent_status`).  The permission-required event is
        sent ``high_priority`` so the OS does not defer the time-sensitive one.
        """
        if device.get("push_provider") != "fcm":
            return PushDeliveryResult(status="unsupported_provider")
        token = device.get("push_token")
        if not isinstance(token, str) or not token:
            return PushDeliveryResult(status="missing_token")
        return await self._post_fcm_message(
            token=token,
            data={k: str(v) for k, v in data.items()},
            high_priority=high_priority,
        )


def get_push_sender(state: Any, config: Config) -> FcmPushSender:
    sender = (
        state.get("android_push_sender")
        if isinstance(state, dict)
        else getattr(state, "android_push_sender", None)
    )
    if sender is None:
        sender = FcmPushSender(config)
        if isinstance(state, dict):
            state["android_push_sender"] = sender
        else:
            state.android_push_sender = sender
    return sender
