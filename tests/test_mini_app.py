"""No Mini App command may ever render a button that does nothing.

Telegram loads a Mini App from its own servers, so a base URL only this
machine can reach produces a perfectly ordinary-looking button that silently
does nothing when tapped — which is what a quick tunnel that failed to start
leaves behind, and which a beginner cannot tell apart from a broken product.

So the commands are driven here rather than the helper alone: the guard is
worth nothing if a handler can route around it, and a *future* handler that
forgets it entirely must still fail safe.  The last test in the file is the
one that enforces that, by refusing to let any call site build a Mini App URL
out of anything but the one function that can answer "no".
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    SandboxConfig,
    TelegramConfig,
)
from open_shrimp.db import init_db
from open_shrimp.handlers.commands import (
    config_handler,
    login_handler,
    review_handler,
    vnc_handler,
)
from open_shrimp.mini_app import mini_app_keyboard, reply_mini_app
from open_shrimp.web_url import mini_app_base

CHAT_ID = 100
USER_ID = 1
REACHABLE = "https://tidy-otter-plays.trycloudflare.com"


def _config(*, public_url: str | None = None, computer_use: bool = False) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="test",
                allowed_tools=[],
                sandbox=(
                    SandboxConfig(backend="docker", computer_use=True)
                    if computer_use
                    else None
                ),
            ),
        },
        default_context="default",
        review=ReviewConfig(public_url=public_url),
    )


class _StubMessage:
    def __init__(self) -> None:
        self.chat_id = CHAT_ID
        self.message_thread_id = None
        self.replies: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def reply_text(
        self,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_: Any,
    ) -> None:
        self.replies.append((text, reply_markup))


class _StubChat:
    PRIVATE = "private"

    def __init__(self, chat_type: str = "private") -> None:
        self.type = chat_type
        self.id = CHAT_ID


class _StubUpdate:
    def __init__(self, chat_type: str = "private") -> None:
        self.message = _StubMessage()
        self.effective_message = self.message
        self.effective_chat = _StubChat(chat_type)
        self.effective_user = type("U", (), {"id": USER_ID})()


class _StubContext:
    def __init__(self, config: Config, db: Any = None) -> None:
        self.bot_data: dict[str, Any] = {"config": config}
        if db is not None:
            self.bot_data["db"] = db
        self.args: list[str] = []


@asynccontextmanager
async def _maybe_db(tmp_path: Path, needed: bool):
    """A live session DB for the handlers that read the active context."""
    if not needed:
        yield None
        return
    connection = await init_db(tmp_path / "sessions.db")
    try:
        yield connection
    finally:
        await connection.close()


def _only_reply(update: _StubUpdate) -> tuple[str, InlineKeyboardMarkup | None]:
    assert len(update.message.replies) == 1
    return update.message.replies[0]


def _urls(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        (button.web_app.url if button.web_app else button.url)
        for row in markup.inline_keyboard
        for button in row
    ]


class TestMiniAppBase:
    """The one function allowed to yield a base for a Mini App URL."""

    def test_a_reachable_public_url_is_a_base(self) -> None:
        assert mini_app_base(_config(public_url=REACHABLE)) == REACHABLE

    def test_the_host_port_fallback_is_not_a_base(self) -> None:
        """``public_base`` still answers here, which is exactly the trap: the
        caller gets a plausible ``https://127.0.0.1:8080`` and no signal."""
        assert mini_app_base(_config()) is None

    def test_an_address_only_this_network_can_reach_is_not_a_base(self) -> None:
        assert mini_app_base(_config(public_url="https://192.168.1.40:8080")) is None


class TestKeyboard:
    def test_a_reachable_base_builds_the_buttons(self) -> None:
        markup = mini_app_keyboard(
            [[("Open login", "/terminal/?mode=login")]],
            config=_config(public_url=REACHABLE),
            chat_id=CHAT_ID,
            user_id=USER_ID,
            is_private_chat=True,
        )

        assert markup is not None
        assert _urls(markup) == [f"{REACHABLE}/terminal/?mode=login"]

    def test_no_base_means_no_keyboard_at_all(self) -> None:
        """``None`` and not an empty markup: a caller who forgets the
        distinction passes it straight to ``reply_markup`` and renders a
        plain message, which is safe.  A dead button is not."""
        assert (
            mini_app_keyboard(
                [[("Open login", "/terminal/?mode=login")]],
                config=_config(),
                chat_id=CHAT_ID,
                user_id=USER_ID,
                is_private_chat=True,
            )
            is None
        )

    def test_a_group_chat_still_gets_a_token_url_and_is_still_guarded(self) -> None:
        """The group fallback is a plain ``url`` button rather than a
        ``WebAppInfo``, and Telegram is no more able to reach loopback for it."""
        reachable = mini_app_keyboard(
            [[("Open Review", "/app/?chat_id=1")]],
            config=_config(public_url=REACHABLE),
            chat_id=CHAT_ID,
            user_id=USER_ID,
            is_private_chat=False,
        )
        assert reachable is not None
        assert reachable.inline_keyboard[0][0].web_app is None
        assert "token=" in _urls(reachable)[0]

        assert (
            mini_app_keyboard(
                [[("Open Review", "/app/?chat_id=1")]],
                config=_config(),
                chat_id=CHAT_ID,
                user_id=USER_ID,
                is_private_chat=False,
            )
            is None
        )


class TestReplyMiniApp:
    @pytest.mark.asyncio
    async def test_the_button_is_sent_when_telegram_can_open_it(self) -> None:
        message = _StubMessage()
        await reply_mini_app(
            message,
            text="Sign in",
            rows=[[("Open login", "/terminal/?mode=login")]],
            config=_config(public_url=REACHABLE),
            user_id=USER_ID,
            is_private_chat=True,
            opens="the sign-in page",
            still_works="Chatting still works.",
        )

        text, markup = message.replies[0]
        assert text == "Sign in"
        assert markup is not None

    @pytest.mark.asyncio
    async def test_an_explanation_is_sent_instead_of_a_dead_button(self) -> None:
        message = _StubMessage()
        await reply_mini_app(
            message,
            text="Sign in",
            rows=[[("Open login", "/terminal/?mode=login")]],
            config=_config(),
            user_id=USER_ID,
            is_private_chat=True,
            opens="the sign-in page",
            still_works="Chatting with me here still works.",
        )

        text, markup = message.replies[0]
        assert markup is None
        assert "the sign-in page" in text
        assert "Chatting with me here still works." in text

    @pytest.mark.asyncio
    async def test_the_explanation_says_what_to_do_and_not_only_what_broke(
        self,
    ) -> None:
        """A refusal a beginner cannot act on is the failure this replaces,
        so both remedies the readiness card names have to appear here too."""
        message = _StubMessage()
        await reply_mini_app(
            message,
            text="Sign in",
            rows=[[("Open login", "/terminal/?mode=login")]],
            config=_config(),
            user_id=USER_ID,
            is_private_chat=True,
            opens="the sign-in page",
            still_works="Chatting still works.",
        )

        text, _ = message.replies[0]
        assert "restart" in text.lower()
        assert "review.public_url" in text
        # No jargon the person reading it has not been given a meaning for.
        assert "tunnel" in text and "Telegram" in text


class TestCommandsAreGuarded:
    """Driving the real handlers, because the guard is only worth what the
    commands actually do with it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,needs_db,computer_use",
        [
            (login_handler, False, False),
            (config_handler, False, False),
            (review_handler, True, False),
            (vnc_handler, True, True),
        ],
        ids=["login", "config", "review", "vnc"],
    )
    async def test_a_reachable_base_still_renders_the_button(
        self, tmp_path: Path, handler, needs_db: bool, computer_use: bool
    ) -> None:
        config = _config(public_url=REACHABLE, computer_use=computer_use)
        update = _StubUpdate()

        async with _maybe_db(tmp_path, needs_db) as connection:
            await handler(update, _StubContext(config, connection))

        _, markup = _only_reply(update)
        assert markup is not None
        assert all(url.startswith(REACHABLE) for url in _urls(markup))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,needs_db,computer_use",
        [
            (login_handler, False, False),
            (config_handler, False, False),
            (review_handler, True, False),
            (vnc_handler, True, True),
        ],
        ids=["login", "config", "review", "vnc"],
    )
    async def test_no_reachable_base_renders_text_and_no_button(
        self, tmp_path: Path, handler, needs_db: bool, computer_use: bool
    ) -> None:
        config = _config(computer_use=computer_use)
        update = _StubUpdate()

        async with _maybe_db(tmp_path, needs_db) as connection:
            await handler(update, _StubContext(config, connection))

        text, markup = _only_reply(update)
        assert markup is None
        assert "Mini App" in text
        assert "review.public_url" in text

    @pytest.mark.asyncio
    async def test_review_with_several_directories_is_guarded_too(
        self, tmp_path: Path
    ) -> None:
        """The multi-directory branch builds its own row per directory, which
        is exactly the sort of second path a per-handler check gets bolted
        onto only once."""
        config = _config()
        config.contexts["default"].additional_directories = ["/tmp/a", "/tmp/b"]
        update = _StubUpdate()

        async with _maybe_db(tmp_path, True) as connection:
            await review_handler(update, _StubContext(config, connection))

        text, markup = _only_reply(update)
        assert markup is None
        assert "Mini App" in text


def test_no_call_site_rebuilds_a_mini_app_base_by_hand() -> None:
    """The guard holds only while ``mini_app_base`` is the sole way to get one.

    Eleven call sites once pasted this expression inline, which is why a check
    bolted onto one command would not have held — and why a new one appearing
    should fail here rather than in a user's chat, months later, as a button
    that does nothing.
    """
    inline = re.compile(r"review\.public_url")
    allowed = {
        "web_url.py",  # defines public_base / is_public_base / mini_app_base
        "config.py",  # parses and serialises the field
        "main.py",  # assigns the tunnel's URL to it at boot
        "readiness.py",  # names it in the remedy it prints
        "mini_app.py",  # names it in the remedy it prints
    }

    offenders = sorted(
        str(path.relative_to(Path(__file__).parent.parent))
        for path in Path(__file__).parent.parent.joinpath("src").rglob("*.py")
        if path.name not in allowed and inline.search(path.read_text())
    )

    assert offenders == []
