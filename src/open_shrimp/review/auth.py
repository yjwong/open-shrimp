"""Telegram initData authentication for the review web app.

Supports two auth schemes:
- ``tg-init-data <initData>`` — standard Telegram Mini App auth
- ``tg-token <token>`` — HMAC token for non-Mini-App contexts (group chats)
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, unquote


class AuthError(Exception):
    """Authentication error with HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# HMAC token generation / validation (for group chat URL buttons)
# ---------------------------------------------------------------------------

_TOKEN_TTL = 3600  # 1 hour, matches initData expiry


def generate_auth_token(
    user_id: int, chat_id: int, bot_token: str, ttl: int = _TOKEN_TTL
) -> str:
    """Generate a short-lived HMAC-SHA256 auth token.

    Format: ``user_id:chat_id:expiry_ts:hmac_hex``
    """
    expiry_ts = int(time.time()) + ttl
    payload = f"{user_id}:{chat_id}:{expiry_ts}"
    sig = hmac.new(
        bot_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{sig}"


def validate_auth_token(
    token: str, bot_token: str, allowed_users: list[int]
) -> int:
    """Validate an HMAC auth token and return the user ID.

    Raises AuthError on failure.
    """
    parts = token.split(":")
    if len(parts) != 4:
        raise AuthError(401, "Malformed auth token")

    try:
        user_id = int(parts[0])
        # parts[1] is chat_id — not checked server-side beyond HMAC integrity
        expiry_ts = int(parts[2])
    except ValueError:
        raise AuthError(401, "Invalid auth token fields")

    provided_sig = parts[3]
    payload = f"{parts[0]}:{parts[1]}:{parts[2]}"
    expected_sig = hmac.new(
        bot_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise AuthError(401, "Invalid auth token signature")

    if int(time.time()) > expiry_ts:
        raise AuthError(401, "Auth token has expired")

    if user_id not in allowed_users:
        raise AuthError(403, "User not allowed")

    return user_id


def _compute_data_check_string(parsed: dict[str, list[str]]) -> str:
    """Build the data_check_string per Telegram's spec.

    Takes all key=value pairs except ``hash``, sorts alphabetically by key,
    and joins with newline.  Values are the *raw* (already URL-decoded) strings
    that ``parse_qs`` returns.
    """
    pairs: list[str] = []
    for key in sorted(parsed):
        if key == "hash":
            continue
        # parse_qs returns lists; initData keys are unique so take the first.
        pairs.append(f"{key}={parsed[key][0]}")
    return "\n".join(pairs)


def _verify_hmac(data_check_string: str, provided_hash: str, bot_token: str) -> bool:
    """Verify the HMAC-SHA-256 signature of the initData."""
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, provided_hash)


async def validate_init_data(
    authorization: str, bot_token: str, allowed_users: list[int]
) -> int:
    """Validate a Telegram ``initData`` string from an Authorization header.

    Parameters
    ----------
    authorization:
        The full ``Authorization`` header value, expected in the form
        ``tg-init-data <initData>``.
    bot_token:
        The Telegram bot token used to derive the HMAC secret.
    allowed_users:
        List of Telegram user IDs permitted to use the app.

    Returns
    -------
    int
        The authenticated Telegram user ID.

    Raises
    ------
    AuthError
        On any validation failure (bad header, invalid HMAC, expired, or
        unauthorised user).
    """
    # --- Parse the Authorization header ---
    if not authorization:
        raise AuthError(401, "Missing Authorization header")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0] != "tg-init-data":
        raise AuthError(401, "Malformed Authorization header")

    init_data_raw: str = parts[1]

    # parse_qs URL-decodes values automatically.
    parsed = parse_qs(init_data_raw, keep_blank_values=True)

    if "hash" not in parsed:
        raise AuthError(401, "Missing hash in initData")

    provided_hash: str = parsed["hash"][0]

    # --- HMAC verification ---
    data_check_string = _compute_data_check_string(parsed)

    if not _verify_hmac(data_check_string, provided_hash, bot_token):
        raise AuthError(401, "Invalid initData signature")

    # --- auth_date freshness ---
    if "auth_date" not in parsed:
        raise AuthError(401, "Missing auth_date in initData")

    try:
        auth_date = int(parsed["auth_date"][0])
    except (ValueError, IndexError):
        raise AuthError(401, "Invalid auth_date in initData")

    now = int(time.time())
    if now - auth_date > 3600:
        raise AuthError(401, "initData has expired")

    # --- Extract and authorise user ---
    if "user" not in parsed:
        raise AuthError(401, "Missing user in initData")

    try:
        user_obj = json.loads(parsed["user"][0])
        user_id = int(user_obj["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise AuthError(401, "Invalid user data in initData")

    if user_id not in allowed_users:
        raise AuthError(403, "User not allowed")

    return user_id


# ---------------------------------------------------------------------------
# Unified authentication
# ---------------------------------------------------------------------------


async def authenticate(
    authorization: str, bot_token: str, allowed_users: list[int]
) -> int:
    """Validate an Authorization header using either auth scheme.

    Accepts:
    - ``tg-init-data <initData>`` — Telegram Mini App initData
    - ``tg-token <token>`` — HMAC auth token (for group chat URL buttons)

    Returns the authenticated user ID.  Raises AuthError on failure.
    """
    if not authorization:
        raise AuthError(401, "Missing Authorization header")

    if authorization.startswith("tg-init-data "):
        return await validate_init_data(authorization, bot_token, allowed_users)
    elif authorization.startswith("tg-token "):
        token = authorization.split(" ", 1)[1]
        return validate_auth_token(token, bot_token, allowed_users)
    else:
        raise AuthError(401, "Unsupported auth scheme")


async def validate_token_param(
    token: str, bot_token: str, allowed_users: list[int]
) -> int:
    """Validate a token query parameter that could be either initData or HMAC.

    HMAC tokens have the format ``user_id:chat_id:expiry_ts:sig`` (4 colon-
    separated parts).  Telegram initData is URL-encoded key-value pairs.
    """
    if token.count(":") == 3:
        return validate_auth_token(token, bot_token, allowed_users)
    return await validate_init_data(
        f"tg-init-data {token}", bot_token, allowed_users
    )
