"""The card that replaces the START press the wizard spends.

Telegram shows START once per chat.  The enrollment handshake consumes it, and
nobody types ``/start`` at a bot they have already started, so the product's
only explanation of itself has to arrive unprompted on first boot instead.

The checks themselves live in ``test_readiness.py``; what is under test here is
delivery — who is told, how often, and what a failed send costs.
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
from open_shrimp.first_boot import ORIENTATION, orientation_text, send_first_boot
from open_shrimp.readiness import Row, State


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


_ALL_WELL = [
    Row("sign_in", "Signed in to Claude", State.OK),
    Row("project", "Project folder", State.OK),
]


def _rows(monkeypatch, rows: list[Row]) -> None:
    async def _check(config: Config) -> list[Row]:
        return list(rows)

    monkeypatch.setattr("open_shrimp.first_boot.check_readiness", _check)


@pytest.fixture(autouse=True)
def _healthy(monkeypatch) -> None:
    """Nothing wrong unless a test says so, so onceness is what is measured."""
    _rows(monkeypatch, _ALL_WELL)


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


def _text_of(bot) -> str:
    return bot.send_message.await_args.kwargs["text"]


class TestDelivery:
    @pytest.mark.asyncio
    async def test_every_allowed_user_is_told(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        bot = _bot()
        try:
            await send_first_boot(bot, db, _config())
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
            sent = await send_first_boot(bot, db, _config())
        finally:
            await db.close()

        assert bot.send_message.await_count == 2
        assert sent == 1

    @pytest.mark.asyncio
    async def test_the_first_card_carries_both_halves(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        bot = _bot()
        try:
            await send_first_boot(bot, db, _config())
        finally:
            await db.close()

        text = _text_of(bot)
        assert "OpenShrimp is running." in text
        assert "Signed in to Claude" in text


class TestOnceness:
    @pytest.mark.asyncio
    async def test_a_second_boot_says_nothing(self, tmp_path: Path) -> None:
        db = await init_db(tmp_path / "sessions.db")
        try:
            assert await send_first_boot(_bot(), db, _config()) == 2
            second = _bot()
            assert await send_first_boot(second, db, _config()) == 0
            second.send_message.assert_not_awaited()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions.db"
        db = await init_db(path)
        await send_first_boot(_bot(), db, _config())
        await db.close()

        db = await init_db(path)
        again = _bot()
        try:
            assert await send_first_boot(again, db, _config()) == 0
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
            await send_first_boot(_bot(side_effect=[Forbidden("blocked"), None]), db, _config())
            retry = _bot()
            assert await send_first_boot(retry, db, _config()) == 1
            assert retry.send_message.await_args.kwargs["chat_id"] == 11
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_new_operator_is_oriented(self, tmp_path: Path) -> None:
        """Re-running the wizard for a different account must not leave the new
        one with no explanation."""
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            moved = _config()
            object.__setattr__(moved, "allowed_users", [11, 33])
            fresh = _bot()
            assert await send_first_boot(fresh, db, moved) == 1
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


_TUNNEL_DOWN = [
    Row("sign_in", "Signed in to Claude", State.OK),
    Row("mini_apps", "Mini Apps open", State.PROBLEM, "the tunnel didn't start"),
]

_TUNNEL_UNKNOWN = [
    Row("sign_in", "Signed in to Claude", State.OK),
    Row("mini_apps", "Mini Apps open", State.UNKNOWN, "this check couldn't run."),
]


class TestRegressions:
    @pytest.mark.asyncio
    async def test_a_standing_problem_is_not_repeated(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Restarting a bot with a broken tunnel five times must not produce
        five identical cards, or the card stops being read."""
        _rows(monkeypatch, _TUNNEL_DOWN)
        db = await init_db(tmp_path / "sessions.db")
        try:
            assert await send_first_boot(_bot(), db, _config()) == 2
            assert await send_first_boot(_bot(), db, _config()) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_new_problem_is_reported_without_the_orientation(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            _rows(monkeypatch, _TUNNEL_DOWN)
            broke = _bot()
            assert await send_first_boot(broke, db, _config()) == 2
        finally:
            await db.close()

        text = _text_of(broke)
        assert "OpenShrimp is running." not in text
        assert "the tunnel didn't start" in text

    @pytest.mark.asyncio
    async def test_a_problem_that_comes_back_is_news_again(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The claim is given back when the row passes, so a tunnel that dies
        a month later is reported rather than remembered as already-said."""
        _rows(monkeypatch, _TUNNEL_DOWN)
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            _rows(monkeypatch, _ALL_WELL)
            assert await send_first_boot(_bot(), db, _config()) == 0

            _rows(monkeypatch, _TUNNEL_DOWN)
            assert await send_first_boot(_bot(), db, _config()) == 2
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_fixed_problem_alone_is_not_worth_a_card(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Good news is not news: the operator who just fixed it knows."""
        _rows(monkeypatch, _TUNNEL_DOWN)
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            _rows(monkeypatch, _ALL_WELL)
            quiet = _bot()
            assert await send_first_boot(quiet, db, _config()) == 0
            quiet.send_message.assert_not_awaited()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_check_that_could_not_run_changes_nothing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A flaky probe must not hand the claim back: the next boot would
        re-announce a standing failure as news, on every flap."""
        _rows(monkeypatch, _TUNNEL_DOWN)
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            _rows(monkeypatch, _TUNNEL_UNKNOWN)
            quiet = _bot()
            assert await send_first_boot(quiet, db, _config()) == 0

            _rows(monkeypatch, _TUNNEL_DOWN)
            assert await send_first_boot(_bot(), db, _config()) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_an_unknown_row_is_not_news_on_its_own(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Nobody can act on "I couldn't check", so it does not earn a DM."""
        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())

            _rows(monkeypatch, _TUNNEL_UNKNOWN)
            quiet = _bot()
            assert await send_first_boot(quiet, db, _config()) == 0
            quiet.send_message.assert_not_awaited()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_worst_install_still_gets_a_card(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Telegram refuses an over-long message outright, and the install
        that produces one is the install that needs the card most."""
        _rows(
            monkeypatch,
            [Row("sandbox", "Sandbox ready", State.PROBLEM, "x" * 20000)] * 20,
        )
        db = await init_db(tmp_path / "sessions.db")
        bot = _bot()
        try:
            await send_first_boot(bot, db, _config())
        finally:
            await db.close()

        assert len(_text_of(bot)) <= 4096

    @pytest.mark.asyncio
    async def test_a_problem_whose_card_never_arrived_is_still_new(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The claim is the record of having been *told*, not of having
        checked, so a send that failed must not count as one."""
        from telegram.error import Forbidden

        db = await init_db(tmp_path / "sessions.db")
        try:
            await send_first_boot(_bot(), db, _config())  # spends the orientation

            _rows(monkeypatch, _TUNNEL_DOWN)
            await send_first_boot(
                _bot(side_effect=[Forbidden("blocked"), None]), db, _config()
            )

            retry = _bot()
            assert await send_first_boot(retry, db, _config()) == 1
            assert retry.send_message.await_args.kwargs["chat_id"] == 11
        finally:
            await db.close()
