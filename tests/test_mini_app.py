"""No Mini App command may ever render a button that does nothing.

Telegram loads a Mini App from its own servers, so a base URL only this
machine can reach produces a perfectly ordinary-looking button that silently
does nothing when tapped — which is what a quick tunnel that failed to start
leaves behind, and which a beginner cannot tell apart from a broken product.

So the commands are driven here rather than the helper alone: the guard is
worth nothing if a handler can route around it, and a *future* handler that
forgets it entirely must still fail safe.  The last test in the file enforces
that structurally, by keeping the unsafe primitive out of reach.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
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
from open_shrimp.mini_app import (
    _unavailable_text,
    make_web_app_button,
    mini_app_keyboard,
    reply_mini_app,
)
from open_shrimp.web_url import mini_app_base

CHAT_ID = 100
USER_ID = 1
REACHABLE = "https://tidy-otter-plays.trycloudflare.com"

# Every command that hands out a Mini App.  A new one belongs here.
MINI_APP_COMMANDS = [
    pytest.param(login_handler, id="login"),
    pytest.param(config_handler, id="config"),
    pytest.param(review_handler, id="review"),
    pytest.param(vnc_handler, id="vnc"),
]


def _config(*, public_url: str | None = None) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="test",
                allowed_tools=[],
                # /vnc needs it; nothing else looks.
                sandbox=SandboxConfig(backend="docker", computer_use=True),
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


class _StubUpdate:
    def __init__(self) -> None:
        self.message = _StubMessage()
        self.effective_message = self.message
        self.effective_chat = type("C", (), {"PRIVATE": "private", "type": "private"})()
        self.effective_user = type("U", (), {"id": USER_ID})()


class _StubContext:
    def __init__(self, config: Config, db: Any) -> None:
        self.bot_data: dict[str, Any] = {"config": config, "db": db}
        self.args: list[str] = []


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    connection = await init_db(tmp_path / "sessions.db")
    yield connection
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


class TestButton:
    """``base | None`` in, ``button | None`` out — the guard as a type."""

    def test_a_base_and_a_path_make_a_button(self) -> None:
        button = make_web_app_button(
            "Open login",
            REACHABLE,
            "/terminal/?mode=login",
            chat_id=CHAT_ID,
            user_id=USER_ID,
            bot_token="0:fake",
            is_private_chat=True,
        )

        assert button is not None
        assert button.web_app.url == f"{REACHABLE}/terminal/?mode=login"

    def test_no_base_makes_no_button(self) -> None:
        """The path cannot be concatenated onto a base that does not exist,
        so a caller cannot reach a URL without first passing the guard."""
        assert (
            make_web_app_button(
                "Open login",
                None,
                "/terminal/?mode=login",
                chat_id=CHAT_ID,
                user_id=USER_ID,
                bot_token="0:fake",
                is_private_chat=True,
            )
            is None
        )


class TestKeyboard:
    def test_a_reachable_base_builds_the_buttons(self) -> None:
        markup = mini_app_keyboard(
            [("Open login", "/terminal/?mode=login")],
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
                [("Open login", "/terminal/?mode=login")],
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
            [("Open Review", "/app/?chat_id=1")],
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
                [("Open Review", "/app/?chat_id=1")],
                config=_config(),
                chat_id=CHAT_ID,
                user_id=USER_ID,
                is_private_chat=False,
            )
            is None
        )


class TestUnavailableText:
    """A refusal a beginner cannot act on is the failure this replaces."""

    def test_it_names_the_thing_what_still_works_and_both_remedies(self) -> None:
        text = _unavailable_text("the sign-in page", "Chatting still works.")

        assert "the sign-in page" in text
        assert "Chatting still works." in text
        assert "restart" in text.lower()
        # The same two remedies the readiness card offers.
        assert "review.public_url" in text
        assert "tunnel" in text and "Telegram" in text


class TestReplyMiniApp:
    @pytest.mark.asyncio
    async def test_the_button_is_sent_when_telegram_can_open_it(self) -> None:
        message = _StubMessage()
        await reply_mini_app(
            message,
            text="Sign in",
            buttons=[("Open login", "/terminal/?mode=login")],
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
            buttons=[("Open login", "/terminal/?mode=login")],
            config=_config(),
            user_id=USER_ID,
            is_private_chat=True,
            opens="the sign-in page",
            still_works="Chatting with me here still works.",
        )

        text, markup = message.replies[0]
        assert markup is None
        assert text == _unavailable_text(
            "the sign-in page", "Chatting with me here still works."
        )


class TestCommandsAreGuarded:
    """Driving the real handlers, because the guard is only worth what the
    commands actually do with it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", MINI_APP_COMMANDS)
    async def test_a_reachable_base_still_renders_the_button(
        self, db, handler
    ) -> None:
        update = _StubUpdate()

        await handler(update, _StubContext(_config(public_url=REACHABLE), db))

        _, markup = _only_reply(update)
        assert markup is not None
        assert all(url.startswith(REACHABLE) for url in _urls(markup))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", MINI_APP_COMMANDS)
    async def test_no_reachable_base_renders_text_and_no_button(
        self, db, handler
    ) -> None:
        update = _StubUpdate()

        await handler(update, _StubContext(_config(), db))

        text, markup = _only_reply(update)
        assert markup is None
        assert "Mini App" in text
        assert "review.public_url" in text

    @pytest.mark.asyncio
    async def test_review_with_several_directories_is_guarded_too(self, db) -> None:
        """The multi-directory branch builds its own button per directory,
        which is exactly the sort of second path a per-handler check gets
        bolted onto only once."""
        config = _config()
        config.contexts["default"].additional_directories = ["/tmp/a", "/tmp/b"]
        update = _StubUpdate()

        await review_handler(update, _StubContext(config, db))

        text, markup = _only_reply(update)
        assert markup is None
        assert "Mini App" in text


def test_public_base_stays_out_of_reach_of_the_button_builders() -> None:
    """The guard holds only while ``mini_app_base`` is the sole way to a base.

    ``public_base`` is the unsafe primitive — it answers with a loopback
    address rather than ``None`` — so a module that imports it can hand one to
    ``make_web_app_button`` and get a live button pointing nowhere.  Keeping
    the import out of the button-building modules is what makes the type
    signature mean something.

    Parsed rather than grepped: the modules that explain this failure in prose
    name ``public_base`` in a docstring, and a text search cannot tell that
    apart from a call.
    """
    # Everything below reaches a base through ``mini_app_base``.  These four
    # need the raw one for URLs Telegram never opens: the Android companion
    # deep link, the security-key phone relay, the readiness probe's own HTTP
    # request, and the module that defines it.
    allowed = {
        "web_url.py",
        "readiness.py",
        "security_key/api.py",
        "handlers/commands.py",
    }
    source = Path(__file__).parent.parent / "src"

    offenders = []
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source / "open_shrimp")
        if str(relative) in allowed or relative.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "public_base" for alias in node.names
            ):
                offenders.append(str(relative))

    assert offenders == []
