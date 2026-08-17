"""What the bot does with a message from somebody who is not allowed.

Nothing, is the answer — plus a log line, plus an at-most-hourly note to the
people who *are* allowed.  A reply naming the sender's own id would make a
mistyped allowlist self-diagnosing, but it also confirms to anyone sweeping
the username namespace that a live instance with a real machine sits here, and
hands them the exact instruction to social-engineer the operator with.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, MessageReactionUpdated, Update, User

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.handlers import turned_away
from open_shrimp.handlers.commands import start_handler

ALLOWED = 1
STRANGER = 999


def _config(allowed: list[int] | None = None) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[ALLOWED] if allowed is None else allowed,
        contexts={
            "default": ContextConfig(
                directory="/tmp", description="test", allowed_tools=[]
            )
        },
        default_context="default",
        review=ReviewConfig(),
    )


def _update(user_id: int, *, chat_type: str = Chat.PRIVATE, text: str = "/start"):
    """A real ``Update``, because what is asserted here is which update *types*
    count as somebody addressing the bot."""
    user = User(id=user_id, is_bot=False, first_name="Ada", username="stranger")
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=message)


def _reaction_update(user_id: int):
    """An update type polling delivers that is not somebody messaging the bot."""
    user = User(id=user_id, is_bot=False, first_name="Ada", username="stranger")
    return Update(
        update_id=2,
        message_reaction=MessageReactionUpdated(
            chat=Chat(id=-100, type=Chat.SUPERGROUP),
            message_id=7,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            old_reaction=(),
            new_reaction=(),
            user=user,
        ),
    )


def _start_update(user_id: int, *, text: str = "/start"):
    """A stand-in for ``start_handler``, whose reply has to be observable."""
    message = SimpleNamespace(
        text=text,
        chat_id=user_id,
        message_thread_id=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="stranger"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        effective_message=message,
    )


def _context(config: Config):
    return SimpleNamespace(
        bot_data={"config": config, "db": None},
        bot=SimpleNamespace(send_message=AsyncMock()),
        args=[],
    )


@pytest.fixture(autouse=True)
def _fresh_tally():
    turned_away.reset_state()
    yield
    turned_away.reset_state()


class TestSilence:
    @pytest.mark.asyncio
    async def test_a_start_payload_from_a_stranger_enrolls_nobody(self) -> None:
        """The regression test for the invariant a helpful change would break.

        Enrollment lives in the wizard.  A start payload read by the running
        bot would be an unauthenticated enrollment endpoint on a public
        username, permanently open.
        """
        config = _config()
        update = _start_update(STRANGER, text="/start some-nonce")
        context = _context(config)
        context.args = ["some-nonce"]

        await start_handler(update, context)

        assert config.allowed_users == [ALLOWED]
        update.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bare_start_from_a_stranger_gets_no_reply(self) -> None:
        config = _config()
        update = _start_update(STRANGER)

        await start_handler(update, _context(config))

        update.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_plain_message_from_a_stranger_gets_no_reply(self) -> None:
        from open_shrimp.handlers.messages import message_handler

        config = _config()
        update = _start_update(STRANGER, text="hello?")
        update.effective_message.photo = None
        update.effective_message.document = None
        update.effective_message.audio = None
        update.effective_message.caption = None
        update.effective_message.location = None
        update.effective_message.voice = None
        update.effective_message.video_note = None
        update.effective_message.media_group_id = None

        await message_handler(update, _context(config))

        update.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_reaction_is_not_a_message(self) -> None:
        """Polling asks for every update type; only contact counts."""
        config = _config()
        context = _context(config)

        await turned_away.note_unauthorized(_reaction_update(STRANGER), context)

        context.bot.send_message.assert_not_awaited()

    def test_the_notice_handler_runs_before_every_other(self) -> None:
        """Registered on its own group, ahead of the handlers that do the drop."""
        from telegram.ext import TypeHandler

        from open_shrimp.bot import build_application

        app = build_application(_config(), db=None, backends=[])
        first = min(app.handlers)
        assert any(
            isinstance(h, TypeHandler) and h.callback is turned_away.note_unauthorized
            for h in app.handlers[first]
        )

    @pytest.mark.asyncio
    async def test_the_attempt_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _config()
        with caplog.at_level(logging.WARNING):
            await turned_away.note_unauthorized(_update(STRANGER), _context(config))

        assert "Turned away user 999" in caplog.text
        assert "private" in caplog.text

    @pytest.mark.asyncio
    async def test_an_allowed_user_is_not_logged_or_notified(self) -> None:
        config = _config()
        context = _context(config)

        await turned_away.note_unauthorized(_update(ALLOWED), context)

        context.bot.send_message.assert_not_awaited()


class TestNotice:
    @pytest.mark.asyncio
    async def test_the_allowed_users_are_told_once_an_hour_at_most(self) -> None:
        config = _config()
        context = _context(config)

        for _ in range(5):
            await turned_away.note_unauthorized(_update(STRANGER), context)

        assert context.bot.send_message.await_count == 1
        text = context.bot.send_message.await_args.kwargs["text"]
        assert str(STRANGER) in text
        assert context.bot.send_message.await_args.kwargs["chat_id"] == ALLOWED

    @pytest.mark.asyncio
    async def test_the_next_note_aggregates_what_was_held_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config()
        context = _context(config)
        clock = [0.0]
        monkeypatch.setattr(turned_away.time, "monotonic", lambda: clock[0])

        await turned_away.note_unauthorized(_update(STRANGER), context)
        await turned_away.note_unauthorized(_update(STRANGER + 1), context)
        await turned_away.note_unauthorized(_update(STRANGER + 2), context)

        clock[0] = turned_away.NOTICE_INTERVAL_SECONDS + 1
        await turned_away.note_unauthorized(_update(STRANGER + 3), context)

        assert context.bot.send_message.await_count == 2
        second = context.bot.send_message.await_args.kwargs["text"]
        assert "3 people" in second
        assert str(STRANGER + 1) in second

    @pytest.mark.asyncio
    async def test_nothing_is_sent_when_there_is_nobody_to_tell(self) -> None:
        """The attempt to tell is itself the disclosure."""
        config = _config(allowed=[])
        context = _context(config)

        await turned_away.note_unauthorized(_update(STRANGER), context)

        context.bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_note_carries_no_button(self) -> None:
        config = _config()
        context = _context(config)

        await turned_away.note_unauthorized(_update(STRANGER), context)

        kwargs = context.bot.send_message.await_args.kwargs
        assert "reply_markup" not in kwargs
