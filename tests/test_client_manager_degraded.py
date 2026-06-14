"""Degraded-mode behaviour for the client manager.

When the MCP proxy fails to start (``mcp_proxy is None``), OpenShrimp tools
cannot be served.  The one-time user warning must fire exactly once per
scope and must never break the turn if the Telegram send fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import open_shrimp.client_manager as cm
from open_shrimp.db import ChatScope

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_warned_set():
    cm._tools_degraded_warned.clear()
    yield
    cm._tools_degraded_warned.clear()


async def test_warns_once_per_scope() -> None:
    bot = AsyncMock()
    scope = ChatScope(chat_id=42, thread_id=None)

    await cm._warn_tools_degraded_once(bot, scope)
    await cm._warn_tools_degraded_once(bot, scope)

    assert bot.send_message.await_count == 1
    args = bot.send_message.await_args.kwargs
    assert args["chat_id"] == 42
    assert "message_thread_id" not in args


async def test_warns_per_distinct_scope() -> None:
    bot = AsyncMock()
    await cm._warn_tools_degraded_once(bot, ChatScope(chat_id=1, thread_id=None))
    await cm._warn_tools_degraded_once(bot, ChatScope(chat_id=2, thread_id=None))
    assert bot.send_message.await_count == 2


async def test_thread_id_passed_for_forum_scope() -> None:
    bot = AsyncMock()
    await cm._warn_tools_degraded_once(bot, ChatScope(chat_id=5, thread_id=9))
    args = bot.send_message.await_args.kwargs
    assert args["message_thread_id"] == 9


async def test_send_failure_is_swallowed() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("telegram down")
    scope = ChatScope(chat_id=7, thread_id=None)

    # Must not raise.
    await cm._warn_tools_degraded_once(bot, scope)

    # Scope is still recorded as warned (we don't retry a failing send).
    assert scope in cm._tools_degraded_warned
