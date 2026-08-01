"""The pending-approval registry, and its two consumers.

A scope awaiting a tap is exempt from the idle sweep; a card whose session
has gone away is retired rather than left looking live.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_shrimp import client_manager
from open_shrimp.db import ChatScope
from open_shrimp.handlers.approval import retire_pending_approvals
from open_shrimp.handlers.state import (
    _pending_approvals,
    has_pending_approval,
    register_pending_approval,
    release_pending_approval,
    take_pending_approvals,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_registry():
    _pending_approvals.clear()
    yield
    _pending_approvals.clear()


def _session(idle_for: float) -> SimpleNamespace:
    return SimpleNamespace(last_activity=time.monotonic() - idle_for)


async def test_idle_sweep_skips_scope_awaiting_approval(monkeypatch) -> None:
    scope = ChatScope(chat_id=1, thread_id=5)
    monkeypatch.setattr(
        client_manager, "_active_sessions",
        {scope: _session(client_manager._IDLE_TIMEOUT + 60)},
    )

    # Idle past the timeout with nothing pending: reaped.
    assert client_manager._stale_scopes(time.monotonic()) == [scope]

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    entry = register_pending_approval(scope, 1, 99, future)

    assert client_manager._stale_scopes(time.monotonic()) == []

    # Once answered, the exemption lapses — a resolved card holds nothing.
    future.set_result(True)
    assert client_manager._stale_scopes(time.monotonic()) == [scope]

    release_pending_approval(scope, entry)
    assert client_manager._stale_scopes(time.monotonic()) == [scope]


async def test_pending_approval_registry_is_per_scope() -> None:
    loop = asyncio.get_running_loop()
    a = ChatScope(chat_id=1, thread_id=1)
    b = ChatScope(chat_id=1, thread_id=2)
    register_pending_approval(a, 1, 10, loop.create_future())

    assert has_pending_approval(a) is True
    assert has_pending_approval(b) is False

    # A scopeless approval (private chat helper without a scope) is ignored
    # rather than mis-filed under some other scope.
    assert register_pending_approval(None, 1, 11, loop.create_future()) is None


async def test_retire_cancels_future_and_strips_buttons() -> None:
    scope = ChatScope(chat_id=1, thread_id=3)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
    )
    register_pending_approval(scope, 1, 42, future, bot=bot, text="Run rm?")

    await retire_pending_approvals(scope)

    assert future.cancelled()
    assert has_pending_approval(scope) is False
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 42
    assert kwargs["reply_markup"] is None
    assert "no longer live" in kwargs["text"]
    # The original card text is preserved above the note.
    assert kwargs["text"].startswith("Run rm?")


async def test_retire_falls_back_to_stripping_markup_on_edit_failure() -> None:
    scope = ChatScope(chat_id=1, thread_id=4)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=RuntimeError("bad markdown")),
        edit_message_reply_markup=AsyncMock(),
    )
    register_pending_approval(scope, 1, 42, future, bot=bot, text="x")

    await retire_pending_approvals(scope)

    # The buttons must go even when the text edit is rejected.
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 42
    assert future.cancelled()


async def test_retire_is_idempotent_and_safe_without_a_card() -> None:
    scope = ChatScope(chat_id=9, thread_id=None)
    await retire_pending_approvals(scope)
    assert take_pending_approvals(scope) == []
