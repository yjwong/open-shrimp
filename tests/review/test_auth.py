"""Tests for Telegram initData authentication."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from open_shrimp.review.auth import AuthError, validate_init_data

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USERS = [111222333]


def _build_init_data(
    bot_token: str = BOT_TOKEN,
    user_id: int = 111222333,
    auth_date: int | None = None,
    tamper_hash: bool = False,
    exclude_hash: bool = False,
    exclude_user: bool = False,
    exclude_auth_date: bool = False,
) -> str:
    """Build a valid (or intentionally invalid) initData query string."""
    if auth_date is None:
        auth_date = int(time.time())

    user_obj = json.dumps(
        {"id": user_id, "first_name": "Test", "username": "testuser"},
        separators=(",", ":"),
    )

    params: dict[str, str] = {}
    if not exclude_auth_date:
        params["auth_date"] = str(auth_date)
    if not exclude_user:
        params["user"] = user_obj
    params["query_id"] = "AAHQ"

    # Build data_check_string the same way Telegram does.
    data_check_string = "\n".join(
        f"{k}={params[k]}" for k in sorted(params)
    )

    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if tamper_hash:
        computed_hash = "a" * 64

    if not exclude_hash:
        params["hash"] = computed_hash

    return urlencode(params)


@pytest.mark.asyncio
async def test_valid_init_data() -> None:
    """A correctly signed initData should return the user ID."""
    init_data = _build_init_data()
    header = f"tg-init-data {init_data}"
    user_id = await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert user_id == 111222333


@pytest.mark.asyncio
async def test_expired_auth_date() -> None:
    """An auth_date older than 1 hour should be rejected."""
    old_time = int(time.time()) - 7200  # 2 hours ago
    init_data = _build_init_data(auth_date=old_time)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_invalid_hmac() -> None:
    """A tampered hash should be rejected."""
    init_data = _build_init_data(tamper_hash=True)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "signature" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_user_not_in_allowed_users() -> None:
    """A user ID not in the allowed list should be rejected with 403."""
    init_data = _build_init_data(user_id=999999999)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 403
    assert "not allowed" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_missing_authorization_header() -> None:
    """An empty authorization string should be rejected."""
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data("", BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_no_prefix() -> None:
    """A header without the tg-init-data prefix should be rejected."""
    init_data = _build_init_data()
    header = f"Bearer {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "malformed" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_malformed_authorization_header_no_space() -> None:
    """A header with no space separator should be rejected."""
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data("tg-init-data", BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_hash_in_init_data() -> None:
    """initData without a hash field should be rejected."""
    init_data = _build_init_data(exclude_hash=True)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "hash" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_missing_user_in_init_data() -> None:
    """initData without a user field should be rejected."""
    init_data = _build_init_data(exclude_user=True)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "user" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_missing_auth_date_in_init_data() -> None:
    """initData without auth_date should be rejected."""
    init_data = _build_init_data(exclude_auth_date=True)
    header = f"tg-init-data {init_data}"
    with pytest.raises(AuthError) as exc_info:
        await validate_init_data(header, BOT_TOKEN, ALLOWED_USERS)
    assert exc_info.value.status_code == 401
    assert "auth_date" in exc_info.value.message.lower()
