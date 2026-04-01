"""Telegram initData authentication for the review web app."""

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
