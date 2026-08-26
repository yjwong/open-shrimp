"""Sign a request the way the paired Android companion does.

Five endpoint test modules authenticate with the same ECDSA-over-P-256
scheme, and five hand-rolled copies of it meant a change to what the
signature covers had to be applied five times or a test quietly stopped
exercising the header it claims to send.  The canonical string is
``METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nBASE64URL(SHA256(body))``; the
verifier is ``android_companion.authenticate_android_request``.
"""

from __future__ import annotations

import base64
import hashlib
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(value: bytes) -> str:
    """Unpadded base64url, the encoding every companion header uses."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def android_headers(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    device_id: str,
    path: str,
    body: bytes = b"",
    method: str = "POST",
    nonce: str = "nonce-1",
) -> dict[str, str]:
    """The four ``X-OpenShrimp-*`` headers for a signed companion request."""
    timestamp = str(int(time.time()))
    body_hash = b64url(hashlib.sha256(body).digest())
    payload = "\n".join([method, path, timestamp, nonce, body_hash]).encode("utf-8")
    return {
        "X-OpenShrimp-Device-Id": device_id,
        "X-OpenShrimp-Timestamp": timestamp,
        "X-OpenShrimp-Nonce": nonce,
        "X-OpenShrimp-Signature": b64url(
            private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
        ),
    }


def public_key_b64(private_key: ec.EllipticCurvePrivateKey) -> str:
    """*private_key*'s public half in the DER/SPKI form pairing expects."""
    from cryptography.hazmat.primitives import serialization

    return b64url(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
