"""Tests for secret entry: the dotenv/file write, the HTTP form API, and the
``request_secret`` tool waiting on it."""

from __future__ import annotations

import asyncio
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.secret_entry import api as secret_api
from open_shrimp.secret_entry.api import (
    create_secret_routes,
    open_request,
    upsert_env,
    write_secret,
)
from open_shrimp.tools import create_openshrimp_tools

from tests.preview.test_api import ALLOWED_USER_ID, BOT_TOKEN, _auth_header


def _config(context_dir: str) -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory=context_dir,
                description="Test context",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


def _client(config: Config) -> AsyncClient:
    app = Starlette(routes=create_secret_routes())
    app.state.config = config
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    )


@pytest.fixture(autouse=True)
def _clear_requests():
    secret_api._requests.clear()
    yield
    secret_api._requests.clear()


# --- upsert_env -------------------------------------------------------------


def test_upsert_appends_to_empty_file() -> None:
    assert upsert_env("", "API_KEY", "abc123") == "API_KEY=abc123\n"


def test_upsert_appends_after_unterminated_last_line() -> None:
    assert upsert_env("A=1", "B", "2") == "A=1\nB=2\n"


def test_upsert_replaces_in_place_and_keeps_export() -> None:
    existing = "# comment\nexport API_KEY=old\nOTHER=x\n"
    assert upsert_env(existing, "API_KEY", "new") == (
        "# comment\nexport API_KEY=new\nOTHER=x\n"
    )


def test_upsert_drops_later_duplicates() -> None:
    existing = "API_KEY=one\nA=1\nAPI_KEY=two\n"
    assert upsert_env(existing, "API_KEY", "new") == "API_KEY=new\nA=1\n"


def test_upsert_does_not_match_key_prefix() -> None:
    assert upsert_env("API_KEY_2=x\n", "API_KEY", "y") == (
        "API_KEY_2=x\nAPI_KEY=y\n"
    )


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ("sk-live_abc/def+==", "sk-live_abc/def+=="),
        ("has space", "'has space'"),
        ("a$b", "'a$b'"),
        ("it's \"x\"\\", '"it\'s \\"x\\"\\\\"'),
    ],
)
def test_upsert_quotes_values(value: str, written: str) -> None:
    assert upsert_env("", "K", value) == f"K={written}\n"


# --- write_secret -----------------------------------------------------------


def test_write_creates_owner_only_file_and_parents(tmp_path) -> None:
    target = tmp_path / "secrets" / "token"
    write_secret(str(target), "-----BEGIN KEY-----\nabc\n", None)
    assert target.read_text() == "-----BEGIN KEY-----\nabc\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert os.listdir(target.parent) == ["token"]


def test_write_keeps_existing_mode(tmp_path) -> None:
    target = tmp_path / ".env"
    target.write_text("A=1\n")
    target.chmod(0o640)
    write_secret(str(target), "v", "B")
    assert target.read_text() == "A=1\nB=v\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


# --- HTTP API ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_writes_and_resolves(tmp_path) -> None:
    target = tmp_path / ".env"
    rid, req = open_request(str(target), "API_KEY", "Stripe key", [str(tmp_path)])

    async with _client(_config(str(tmp_path))) as client:
        resp = await client.get(f"/api/secret/{rid}", headers=_auth_header())
        assert resp.status_code == 200
        assert resp.json() == {
            "path": str(target),
            "env_key": "API_KEY",
            "description": "Stripe key",
            "exists": False,
        }

        resp = await client.post(
            f"/api/secret/{rid}", json={"value": "sk_1"}, headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert req.outcome.result() is True
        assert target.read_text() == "API_KEY=sk_1\n"

        # Single use.
        resp = await client.post(
            f"/api/secret/{rid}", json={"value": "sk_2"}, headers=_auth_header(),
        )
        assert resp.status_code == 404
        assert target.read_text() == "API_KEY=sk_1\n"


@pytest.mark.asyncio
async def test_requires_auth(tmp_path) -> None:
    rid, req = open_request(str(tmp_path / "t"), None, "d", [str(tmp_path)])
    async with _client(_config(str(tmp_path))) as client:
        resp = await client.post(f"/api/secret/{rid}", json={"value": "x"})
    assert resp.status_code == 401
    assert not req.outcome.done()
    assert not (tmp_path / "t").exists()


@pytest.mark.asyncio
async def test_env_value_must_be_one_line(tmp_path) -> None:
    rid, req = open_request(str(tmp_path / ".env"), "K", "d", [str(tmp_path)])
    async with _client(_config(str(tmp_path))) as client:
        resp = await client.post(
            f"/api/secret/{rid}", json={"value": "a\nB=evil"},
            headers=_auth_header(),
        )
    assert resp.status_code == 400
    assert not req.outcome.done()


@pytest.mark.asyncio
async def test_rejects_path_swapped_for_outside_symlink(tmp_path) -> None:
    ctx = tmp_path / "ctx"
    outside = tmp_path / "outside"
    ctx.mkdir()
    outside.mkdir()
    rid, req = open_request(str(ctx / "sub" / "t"), None, "d", [str(ctx)])
    (ctx / "sub").symlink_to(outside)

    async with _client(_config(str(ctx))) as client:
        resp = await client.post(
            f"/api/secret/{rid}", json={"value": "x"}, headers=_auth_header(),
        )
    assert resp.status_code == 403
    assert not (outside / "t").exists()


@pytest.mark.asyncio
async def test_cancel_resolves_false(tmp_path) -> None:
    rid, req = open_request(str(tmp_path / "t"), None, "d", [str(tmp_path)])
    async with _client(_config(str(tmp_path))) as client:
        resp = await client.post(
            f"/api/secret/{rid}/cancel", headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert req.outcome.result() is False
        resp = await client.get(f"/api/secret/{rid}", headers=_auth_header())
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_page_is_served(tmp_path) -> None:
    async with _client(_config(str(tmp_path))) as client:
        resp = await client.get("/secret/")
    assert resp.status_code == 200
    assert "/api/secret/" in resp.text


# --- request_secret tool ----------------------------------------------------


def _tool(config: Config):
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, config=config, context_name="default",
        user_id=ALLOWED_USER_ID,
    )
    return next(t for t in tools if t.name == "request_secret")


@pytest.mark.asyncio
async def test_tool_waits_for_submit_and_never_returns_value(tmp_path) -> None:
    config = _config(str(tmp_path))
    tool = _tool(config)
    target = tmp_path / ".env"
    sent = MagicMock(message_id=5)

    with (
        patch("open_shrimp.tools.mini_app_keyboard", return_value=MagicMock()),
        patch("open_shrimp.tools.send_rich", AsyncMock(return_value=sent)),
        patch("open_shrimp.tools.edit_rich", AsyncMock()) as edit,
    ):
        call = asyncio.create_task(tool.handler({
            "path": str(target), "description": "Key", "env_key": "API_KEY",
        }))
        while not secret_api._requests:
            await asyncio.sleep(0)
        (rid,) = secret_api._requests

        async with _client(config) as client:
            resp = await client.post(
                f"/api/secret/{rid}", json={"value": "sk_secret"},
                headers=_auth_header(),
            )
        assert resp.status_code == 200
        result = await asyncio.wait_for(call, timeout=5)

    text = result["content"][0]["text"]
    assert "API_KEY set in" in text
    assert "sk_secret" not in text
    assert "is_error" not in result
    assert "saved" in edit.await_args.args[3]
    assert target.read_text() == "API_KEY=sk_secret\n"
    assert secret_api._requests == {}


@pytest.mark.asyncio
async def test_tool_rejects_path_outside_context(tmp_path) -> None:
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    tool = _tool(_config(str(ctx)))
    result = await tool.handler({
        "path": str(tmp_path / "elsewhere"), "description": "d",
    })
    assert result["is_error"] is True
    assert secret_api._requests == {}


@pytest.mark.asyncio
async def test_tool_errors_without_mini_app_base(tmp_path) -> None:
    tool = _tool(_config(str(tmp_path)))
    with patch("open_shrimp.tools.mini_app_keyboard", return_value=None):
        result = await tool.handler({
            "path": str(tmp_path / "t"), "description": "d",
        })
    assert result["is_error"] is True
    assert secret_api._requests == {}
