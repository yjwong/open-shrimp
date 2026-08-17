"""The orientation card that replaces the START press the wizard spends.

Telegram shows START once per chat.  The enrollment handshake consumes it, and
nobody types ``/start`` at a bot they have already started, so the product's
only explanation of itself has to arrive unprompted on first boot instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.db import claim_once, init_db
from open_shrimp.first_boot import ORIENTATION, orientation_text, send_orientation


def _config(*, description: str = "the website redesign") -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[11, 22],
        contexts={
            "default": ContextConfig(
                directory="/home/ada/secret-path/projects/site",
                description=description,
                allowed_tools=[],
            )
        },
        default_context="default",
        review=ReviewConfig(),
    )


class TestText:
    def test_it_says_what_this_is_and_how_to_talk_to_it(self) -> None:
        text = orientation_text(_config())

        # What you do with it, and that it asks first.
        assert "send me a message" in text
        assert "asking first" in text
        # Where to look things up.
        assert "/context" in text
        assert "/clear" in text
        assert "/status" in text

    def test_it_names_the_project_not_the_directory(self) -> None:
        text = orientation_text(_config())

        assert "the website redesign" in text
        assert "/home/ada/secret-path" not in text

    def test_it_falls_back_to_the_context_name(self) -> None:
        config = _config(description="")
        assert "default" in orientation_text(config)


def _bot(**kwargs):
    return type("_Bot", (), {"send_message": AsyncMock(**kwargs)})()


class TestDelivery:
    @pytest.mark.asyncio
    async def test_every_allowed_user_is_told(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        bot = _bot()
        try:
            await send_orientation(bot, db, _config())
        finally:
            await db.close()

        assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [
            11,
            22,
        ]

    @pytest.mark.asyncio
    async def test_a_failed_send_does_not_stop_the_others(self, tmp_path: Path) -> None:
        from telegram.error import Forbidden

        db = await init_db(tmp_path / "sessions.db")
        bot = _bot(side_effect=[Forbidden("blocked"), None])
        try:
            sent = await send_orientation(bot, db, _config())
        finally:
            await db.close()

        assert bot.send_message.await_count == 2
        assert sent == 1


class TestOnceness:
    @pytest.mark.asyncio
    async def test_a_second_boot_says_nothing(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        try:
            assert await send_orientation(_bot(), db, _config()) == 2
            second = _bot()
            assert await send_orientation(second, db, _config()) == 0
            second.send_message.assert_not_awaited()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions.db"
        db = await init_db(path)
        await send_orientation(_bot(), db, _config())
        await db.close()

        db = await init_db(path)
        again = _bot()
        try:
            assert await send_orientation(again, db, _config()) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_failed_send_is_retried_on_the_next_boot(
        self, tmp_path: Path
    ) -> None:
        """Losing the product's only explanation of itself to one 502 is worse
        than sending it twice."""
        from telegram.error import Forbidden

        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_orientation(_bot(side_effect=[Forbidden("blocked"), None]), db, _config())
            retry = _bot()
            assert await send_orientation(retry, db, _config()) == 1
            assert retry.send_message.await_args.kwargs["chat_id"] == 11
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_new_operator_is_oriented(self, tmp_path: Path) -> None:
        """Re-running the wizard for a different account must not leave the new
        one with no explanation."""
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_orientation(_bot(), db, _config())

            moved = _config()
            object.__setattr__(moved, "allowed_users", [11, 33])
            fresh = _bot()
            assert await send_orientation(fresh, db, moved) == 1
            assert fresh.send_message.await_args.kwargs["chat_id"] == 33
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_claim_is_per_person(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        try:
            assert await claim_once(db, ORIENTATION, "11")
            assert not await claim_once(db, ORIENTATION, "11")
            assert await claim_once(db, ORIENTATION, "22")
        finally:
            await db.close()
