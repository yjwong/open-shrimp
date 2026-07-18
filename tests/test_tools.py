"""Tests for the transport-neutral OpenShrimp tool descriptors.

Covers the ``create_openshrimp_tools`` factory's gating, the ``read_only``
flags, and handler-body edge cases (direct calls).  Transport-shaped
coverage — the ``/tools/{scope_token}`` JSON-RPC bridge that actually
serves these descriptors — lives in
``tests/mcp_proxy/test_openshrimp_tools.py``.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_shrimp.tools import (
    OpenShrimpTool,
    create_openshrimp_tools,
)


def _names(tools: list[OpenShrimpTool]) -> list[str]:
    return [t.name for t in tools]


def _by_name(tools: list[OpenShrimpTool]) -> dict[str, OpenShrimpTool]:
    return {t.name: t for t in tools}


# --- Gating -----------------------------------------------------------------


def test_minimal_only_send_file() -> None:
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=123)
    assert _names(tools) == ["send_file"]
    assert all(isinstance(t, OpenShrimpTool) for t in tools)


def test_forum_thread_adds_edit_topic() -> None:
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1, thread_id=9)
    assert "edit_topic" in _names(tools)


def test_scheduling_tools_require_db_and_events_config() -> None:
    # No db -> no scheduling tools.
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, config=MagicMock(),
    )
    assert "create_schedule" not in _names(tools)

    # Config without events -> no scheduling tools.
    config = MagicMock()
    config.events = None
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, db=MagicMock(), config=config,
    )
    assert "create_schedule" not in _names(tools)

    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, db=MagicMock(), config=MagicMock(),
    )
    assert {"create_schedule", "list_schedules", "delete_schedule"} <= set(
        _names(tools)
    )


def test_computer_use_tools_require_screenshots_dir() -> None:
    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = None
    sandbox.supports_port_forwarding.return_value = False
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1, sandbox=sandbox)
    assert _names(tools) == ["send_file"]

    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = "/tmp/shots"
    sandbox.supports_port_forwarding.return_value = False
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1, sandbox=sandbox)
    for name in (
        "computer_screenshot", "computer_click", "computer_type",
        "computer_key", "computer_scroll", "computer_toplevel",
    ):
        assert name in _names(tools)


def test_port_forward_gated_on_support() -> None:
    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = None
    sandbox.supports_port_forwarding.return_value = True
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1, sandbox=sandbox)
    assert "port_forward" in _names(tools)


def test_host_bash_gated_on_workdir() -> None:
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1)
    assert "host_bash" not in _names(tools)

    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, host_bash_workdir="/tmp",
    )
    assert "host_bash" in _names(tools)


def test_host_monitor_tools_gated_on_workdir() -> None:
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1)
    assert "host_monitor" not in _names(tools)
    assert "host_monitor_stop" not in _names(tools)

    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, host_bash_workdir="/tmp",
    )
    assert "host_monitor" in _names(tools)
    assert "host_monitor_stop" in _names(tools)


@pytest.mark.asyncio
async def test_host_monitor_schema_validation() -> None:
    host_monitor = _by_name(
        create_openshrimp_tools(
            bot=MagicMock(), chat_id=1, host_bash_workdir="/tmp",
        )
    )["host_monitor"].handler

    # Missing command -> error, no process spawned.
    r = await host_monitor({"description": "x"})
    assert r["is_error"] is True
    assert "command is required" in r["content"][0]["text"]

    # Over-max timeout without persistent -> rejected on the timeout_ms path.
    r = await host_monitor({
        "command": "true", "description": "x", "timeout_ms": 3_600_001,
    })
    assert r["is_error"] is True
    assert "timeout_ms" in r["content"][0]["text"]


# --- read_only flags --------------------------------------------------------


def test_read_only_flags() -> None:
    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = "/tmp/shots"
    sandbox.supports_port_forwarding.return_value = True
    tools = _by_name(create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, thread_id=9,
        db=MagicMock(), config=MagicMock(),
        sandbox=sandbox, host_bash_workdir="/tmp",
    ))

    # Read-only tools.
    assert tools["send_file"].read_only is True
    assert tools["edit_topic"].read_only is True
    assert tools["list_schedules"].read_only is True
    assert tools["computer_screenshot"].read_only is True

    # Mutating tools.
    for name in (
        "create_schedule", "delete_schedule", "computer_click",
        "computer_type", "computer_key", "computer_scroll",
        "computer_toplevel", "port_forward", "host_bash",
        "host_monitor", "host_monitor_stop",
    ):
        assert tools[name].read_only is False, name


# --- handler parity ---------------------------------------------------------


@pytest.mark.asyncio
async def test_send_file_error_paths() -> None:
    send_file = _by_name(
        create_openshrimp_tools(bot=MagicMock(), chat_id=1)
    )["send_file"].handler

    r = await send_file({})
    assert r["is_error"] is True
    assert "file_path is required" in r["content"][0]["text"]

    r = await send_file({"file_path": "/no/such/file/xyz"})
    assert r["is_error"] is True
    assert "File not found" in r["content"][0]["text"]


@pytest.mark.asyncio
async def test_send_file_happy_path() -> None:
    bot = MagicMock()
    bot.send_document = AsyncMock()
    send_file = _by_name(
        create_openshrimp_tools(bot=bot, chat_id=1)
    )["send_file"].handler

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"hello")
        path = f.name
    try:
        r = await send_file({"file_path": path})
        assert "is_error" not in r
        assert "File sent successfully" in r["content"][0]["text"]
        assert bot.send_document.await_count == 1
    finally:
        os.unlink(path)


def _button_config() -> MagicMock:
    config = MagicMock()
    config.review.public_url = "https://miniapps.example.com"
    config.review.host = None
    config.review.port = None
    config.telegram.token = "123:abc"
    return config


async def _send_and_get_markup(suffix: str) -> object:
    """Send a temp file with *suffix* via send_file, return the reply_markup."""
    bot = MagicMock()
    bot.send_document = AsyncMock()
    send_file = _by_name(
        create_openshrimp_tools(bot=bot, chat_id=42, config=_button_config())
    )["send_file"].handler

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(b"content")
        path = f.name
    try:
        r = await send_file({"file_path": path, "type": "document"})
        assert "is_error" not in r
        return bot.send_document.call_args.kwargs["reply_markup"]
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_send_file_pdf_gets_review_button() -> None:
    markup = await _send_and_get_markup(".pdf")
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.text == "📄 Review"
    url = button.web_app.url
    assert url.startswith("https://miniapps.example.com/pdf/?path=")
    assert "chat_id=42" in url


@pytest.mark.asyncio
async def test_send_file_md_gets_preview_button() -> None:
    markup = await _send_and_get_markup(".md")
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.text == "📖 Preview"
    assert button.web_app.url.startswith(
        "https://miniapps.example.com/preview/?path="
    )


@pytest.mark.asyncio
async def test_send_file_other_types_get_no_button() -> None:
    assert await _send_and_get_markup(".txt") is None


@pytest.mark.asyncio
async def test_host_bash_timeout() -> None:
    host_bash = _by_name(
        create_openshrimp_tools(
            bot=MagicMock(), chat_id=1, host_bash_workdir="/tmp",
        )
    )["host_bash"].handler

    r = await host_bash({"command": "sleep 5", "timeout_seconds": 1})
    assert r["is_error"] is True
    assert "timed out" in r["content"][0]["text"]
