"""Per-topic backend selection: ``/backend`` and the state it writes.

Two forum topics bound to the same context are independent scopes, so each
can name its own backend.  The override lives in ``_backend_overrides`` and
reaches the agent through ``_get_context``, which folds it into the copy of
the context config every turn and every card is rendered from — the same
route ``/model`` and ``/effort`` already take.  ``scope_backend_name`` serves
the two pages that deliberately read the unmerged config entry, and
``get_backend_for_scope`` the command gates, which run between a switch and
the next turn when there is no session to read a backend off.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import open_shrimp.client_manager as cm
from open_shrimp.config import Config, ContextConfig, TelegramConfig
from open_shrimp.db import ChatScope, init_db
from open_shrimp.handlers import commands
from open_shrimp.handlers.state import _backend_overrides, _model_overrides
from open_shrimp.handlers.utils import (
    _get_context,
    get_backend_for_scope,
    scope_backend_name,
)
from tests.rich_stub import RichBot, RichMessage, rendered

CHAT_ID = 500
TOPIC_A = ChatScope(chat_id=CHAT_ID, thread_id=11)
TOPIC_B = ChatScope(chat_id=CHAT_ID, thread_id=22)


@pytest.fixture(autouse=True)
def _isolate_state():
    cm._active_sessions.clear()
    _backend_overrides.clear()
    _model_overrides.clear()
    yield
    cm._active_sessions.clear()
    _backend_overrides.clear()
    _model_overrides.clear()


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "dev": ContextConfig(
                directory="/tmp", description="d", allowed_tools=[],
            )
        },
        default_context="dev",
        backend="claude_sdk",
    )


class _StubUpdate:
    def __init__(self, text: str, thread_id: int | None) -> None:
        self.message = RichMessage(text, chat_id=CHAT_ID, thread_id=thread_id)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": 1})()
        self.effective_chat = type(
            "C", (), {"id": CHAT_ID, "type": "private", "PRIVATE": "private"}
        )()


class _StubTelegramContext:
    def __init__(self, db) -> None:
        self.bot_data = {"config": _config(), "db": db}
        self.args: list[str] = []


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


@pytest.fixture
def no_close(monkeypatch: pytest.MonkeyPatch):
    """Stub the session teardown the command performs on every change."""
    closed = AsyncMock()
    monkeypatch.setattr(commands, "close_session", closed)
    return closed


@pytest.mark.asyncio
async def test_command_pins_the_topic_and_closes_the_session(db, no_close):
    update = _StubUpdate("/backend opencode", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    assert _backend_overrides[TOPIC_A] == "opencode"
    no_close.assert_awaited_once()
    assert "opencode" in " ".join(update.message.replies)


@pytest.mark.asyncio
async def test_command_drops_the_model_override_with_it(db, no_close):
    """Aliases are backend-specific, so a stale pin would reach a binary
    that has never heard of the model."""
    _model_overrides[TOPIC_A] = "opus"
    update = _StubUpdate("/backend opencode", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    assert TOPIC_A not in _model_overrides


@pytest.mark.asyncio
async def test_choosing_the_context_default_records_no_override(db, no_close):
    update = _StubUpdate("/backend claude_sdk", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    assert TOPIC_A not in _backend_overrides


@pytest.mark.asyncio
async def test_reset_clears_the_pin(db, no_close):
    _backend_overrides[TOPIC_A] = "opencode"
    update = _StubUpdate("/backend reset", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    assert TOPIC_A not in _backend_overrides
    assert "claude_sdk" in " ".join(update.message.replies)


@pytest.mark.asyncio
async def test_unknown_name_is_refused_without_touching_the_session(db, no_close):
    update = _StubUpdate("/backend gpt", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    assert TOPIC_A not in _backend_overrides
    no_close.assert_not_awaited()
    assert "Unknown backend" in " ".join(update.message.replies)


@pytest.mark.asyncio
async def test_naming_the_backend_already_in_effect_keeps_the_session(db, no_close):
    """A selection that changes nothing must not cost a respawn.

    Closing the client here would make the next turn rebuild the subprocess
    and reconnect the MCP servers to arrive exactly where it already was, and
    would take the scope's /model pin with it.
    """
    _model_overrides[TOPIC_A] = "opus"
    update = _StubUpdate("/backend claude_sdk", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    no_close.assert_not_awaited()
    assert _model_overrides[TOPIC_A] == "opus"
    assert "Already on" in " ".join(update.message.replies)


@pytest.mark.asyncio
async def test_reset_without_a_pin_keeps_the_session(db, no_close):
    update = _StubUpdate("/backend reset", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    no_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_repinning_the_same_backend_keeps_the_session(db, no_close):
    _backend_overrides[TOPIC_A] = "opencode"
    update = _StubUpdate("/backend opencode", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    no_close.assert_not_awaited()
    assert _backend_overrides[TOPIC_A] == "opencode"


@pytest.mark.asyncio
async def test_pin_is_scoped_to_one_topic(db, no_close):
    await commands.backend_handler(
        _StubUpdate("/backend opencode", thread_id=11),
        _StubTelegramContext(db),
    )
    assert _backend_overrides.get(TOPIC_A) == "opencode"
    assert TOPIC_B not in _backend_overrides


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------


class _StubQuery:
    """A CallbackQuery stand-in that records what it answered."""

    def __init__(self, bot: RichBot, thread_id: int | None) -> None:
        self.message = RichMessage(
            "", chat_id=CHAT_ID, thread_id=thread_id, message_id=7, bot=bot,
        )
        self.from_user = type("U", (), {"id": 1})()
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **_: Any) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_picker_lists_every_registered_backend(db, no_close):
    update = _StubUpdate("/backend", thread_id=11)
    await commands.backend_handler(update, _StubTelegramContext(db))

    bot = update.message.bot
    assert len(bot.sends) == 1
    labels = [
        button.text
        for row in bot.sends[0].reply_markup.inline_keyboard
        for button in row
    ]
    assert any("claude_sdk" in label for label in labels)
    assert any("opencode" in label for label in labels)
    no_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_picker_press_pins_and_redraws_in_place(db, no_close):
    bot = RichBot()
    query = _StubQuery(bot, thread_id=11)
    handled = await commands.handle_backend_callback(
        query, "backend:opencode", _config(), _StubTelegramContext(db),
    )
    assert handled
    assert _backend_overrides[TOPIC_A] == "opencode"
    no_close.assert_awaited_once()
    assert not bot.sends, "the press must edit the page, not post a second one"
    assert len(bot.edits) == 1
    assert "override" in rendered(bot.edits[0].text)
    assert query.answers == ["Backend set to opencode"]


@pytest.mark.asyncio
async def test_picker_revert_clears_the_pin(db, no_close):
    _backend_overrides[TOPIC_A] = "opencode"
    bot = RichBot()
    query = _StubQuery(bot, thread_id=11)
    await commands.handle_backend_callback(
        query, "backend_reset", _config(), _StubTelegramContext(db),
    )
    assert TOPIC_A not in _backend_overrides
    assert query.answers == ["Reverted to context default"]


@pytest.mark.asyncio
async def test_picker_press_on_the_current_backend_changes_nothing(db, no_close):
    bot = RichBot()
    query = _StubQuery(bot, thread_id=11)
    await commands.handle_backend_callback(
        query, "backend:claude_sdk", _config(), _StubTelegramContext(db),
    )
    no_close.assert_not_awaited()
    assert not bot.edits, "nothing changed, so there is nothing to redraw"
    assert query.answers == ["Already on claude_sdk"]


@pytest.mark.asyncio
async def test_unrelated_callback_data_is_left_alone(db):
    bot = RichBot()
    query = _StubQuery(bot, thread_id=11)
    assert not await commands.handle_backend_callback(
        query, "model:opus", _config(), _StubTelegramContext(db),
    )


# ---------------------------------------------------------------------------
# What the cards read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_context_folds_the_override_into_the_copy(db):
    """``effective_backend`` on the merged copy names the pinned backend, so
    the status card and every ctx-carrying call site follow without a scope."""
    config = _config()
    _backend_overrides[TOPIC_A] = "opencode"
    resolved = await _get_context(TOPIC_A, config, db)
    assert resolved is not None
    _, ctx = resolved
    assert ctx.backend == "opencode"
    # The raw config entry is untouched — the pages that show override and
    # default side by side read it.
    assert config.contexts["dev"].backend is None


def test_scope_backend_name_reads_the_raw_entry():
    config = _config()
    ctx = config.contexts["dev"]
    assert scope_backend_name(TOPIC_A, ctx, config) == "claude_sdk"
    _backend_overrides[TOPIC_A] = "opencode"
    assert scope_backend_name(TOPIC_A, ctx, config) == "opencode"
    assert scope_backend_name(TOPIC_B, ctx, config) == "claude_sdk"


@pytest.mark.asyncio
async def test_the_merged_copy_is_what_resolve_backend_reads(db):
    """The turn path: _get_context's copy reaches resolve_backend's context
    arm, which beats the process default every handler threads through."""
    from open_shrimp.backend.factory import get_backend_by_name

    config = _config()
    _backend_overrides[TOPIC_A] = "opencode"
    resolved = await _get_context(TOPIC_A, config, db)
    assert resolved is not None
    _, ctx = resolved

    default = get_backend_by_name("claude_sdk")
    assert cm.resolve_backend(default, context=ctx).name == "opencode"


@pytest.mark.asyncio
async def test_two_topics_on_one_context_get_different_backends(db):
    config = _config()
    _backend_overrides[TOPIC_A] = "opencode"

    a = await _get_context(TOPIC_A, config, db)
    b = await _get_context(TOPIC_B, config, db)
    assert a is not None and b is not None
    assert a[1].backend == "opencode"
    assert b[1].backend is None  # falls through to the top-level default
    assert a[0] == b[0] == "dev"  # same context, same working directory


def test_command_gates_follow_the_pin_before_the_next_turn():
    """``/login``, ``/mcp`` and ``/usage`` gate on backend capabilities.

    Between a switch and the next message there is no session to read a
    backend off, so without the override arm the gate would ask the outgoing
    binary what it supports.
    """
    bot_data: dict[str, Any] = {"backend": None}
    _backend_overrides[TOPIC_A] = "opencode"
    assert get_backend_for_scope(bot_data, TOPIC_A).name == "opencode"
    assert get_backend_for_scope(bot_data, TOPIC_B) is None


def _two_context_config() -> Config:
    config = _config()
    config.contexts["other"] = ContextConfig(
        directory="/tmp/other", description="o", allowed_tools=[],
    )
    return config


class _TwoContextTelegramContext(_StubTelegramContext):
    def __init__(self, db) -> None:
        super().__init__(db)
        self.bot_data["config"] = _two_context_config()
        # The switch redraws the pinned status card on its way out.
        self.bot = RichBot()


@pytest.mark.asyncio
async def test_context_switch_by_command_clears_the_pin(db, no_close):
    _backend_overrides[TOPIC_A] = "opencode"
    await commands.context_handler(
        _StubUpdate("/context other", thread_id=11),
        _TwoContextTelegramContext(db),
    )
    assert TOPIC_A not in _backend_overrides


@pytest.mark.asyncio
async def test_context_switch_by_button_clears_the_pin(db, no_close):
    """The picker button must leave the same state ``/context <name>`` does.

    When the two routes each listed the override dicts themselves, the button
    path kept the backend pin and the incoming context's own ``backend:`` was
    silently ignored.
    """
    _backend_overrides[TOPIC_A] = "opencode"
    config = _two_context_config()
    query = _StubQuery(RichBot(), thread_id=11)
    handled = await commands.handle_context_callback(
        query, "ctx:other", config, _TwoContextTelegramContext(db),
    )
    assert handled
    assert TOPIC_A not in _backend_overrides
