"""Surviving an install with no default context and no contexts at all.

A fresh install may finish setup with zero projects configured, so the
config must accept that state and every path that used to reach for
``default_context`` must answer with a sentence instead of raising.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.rich_stub import RichMessage

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
    _parse,
    _validate_raw,
)
from open_shrimp.db import ChatScope, init_db, set_active_context
from open_shrimp.handlers.utils import _get_context, _get_context_name

CHAT_ID = 100
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=None)


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


def _raw(**extra) -> dict:
    raw: dict[str, Any] = {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            }
        },
        "default_context": "default",
    }
    raw.update(extra)
    return raw


def _config(contexts: dict[str, ContextConfig], default: str | None) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts=contexts,
        default_context=default,
        review=ReviewConfig(),
    )


def _empty_config() -> Config:
    return _config({}, None)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_empty_contexts_map_loads():
    raw = _raw(contexts={}, default_context=None)
    _validate_raw(raw)
    cfg = _parse(raw)
    assert cfg.contexts == {}
    assert cfg.default_context is None


def test_null_contexts_is_treated_as_empty():
    raw = _raw(contexts=None, default_context=None)
    _validate_raw(raw)
    assert _parse(raw).contexts == {}


def test_missing_default_context_loads():
    raw = _raw()
    del raw["default_context"]
    _validate_raw(raw)
    assert _parse(raw).default_context is None


def test_contexts_key_is_still_required():
    raw = _raw()
    del raw["contexts"]
    with pytest.raises(ValueError, match="contexts"):
        _validate_raw(raw)


def test_dangling_default_context_is_still_rejected():
    # Relaxing "must be present" must not relax "must name a real context".
    with pytest.raises(ValueError, match="not found in contexts"):
        _validate_raw(_raw(default_context="ghost"))


def test_dangling_default_rejected_even_with_empty_contexts():
    with pytest.raises(ValueError, match="not found in contexts"):
        _validate_raw(_raw(contexts={}, default_context="ghost"))


# ---------------------------------------------------------------------------
# Scope resolution — the KeyError that used to reach the user as silence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dangling_default_resolves_to_none_not_keyerror(db):
    # Constructed directly, bypassing validation, to model a config reloaded
    # after the default's context was deleted.
    config = _config({}, "ghost")
    assert await _get_context_name(SCOPE, config, db) is None
    assert await _get_context(SCOPE, config, db) is None


@pytest.mark.asyncio
async def test_no_default_and_no_contexts_resolves_to_none(db):
    config = _empty_config()
    assert await _get_context_name(SCOPE, config, db) is None
    assert await _get_context(SCOPE, config, db) is None


@pytest.mark.asyncio
async def test_saved_context_still_wins_when_default_is_absent(db):
    config = _config(
        {"proj": ContextConfig(directory="/tmp", description="d", allowed_tools=[])},
        None,
    )
    await set_active_context(db, SCOPE, "proj")
    assert await _get_context_name(SCOPE, config, db) == "proj"


@pytest.mark.asyncio
async def test_stale_saved_context_falls_through_to_none(db):
    config = _empty_config()
    await set_active_context(db, SCOPE, "deleted")
    assert await _get_context_name(SCOPE, config, db) is None


# ---------------------------------------------------------------------------
# Commands answer with words
# ---------------------------------------------------------------------------


class _StubMessage(RichMessage):
    def __init__(self, text: str) -> None:
        super().__init__(text, chat_id=CHAT_ID)


class _StubUpdate:
    def __init__(self, text: str) -> None:
        self.message = _StubMessage(text)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": 1})()
        # Handlers gate on ``chat.type == chat.PRIVATE``, so the stub carries
        # the same constant the real Chat object does.
        self.effective_chat = type(
            "C", (), {"id": CHAT_ID, "type": "private", "PRIVATE": "private"}
        )()


class _StubContext:
    def __init__(self, db) -> None:
        self.bot_data = {"config": _empty_config(), "db": db}
        self.args: list[str] = []


# Each entry is (command text, handler attribute name).  Every command that
# reaches for a context must survive having none.  ``/context`` is absent:
# it is the one command that has something to offer on an empty install (the
# supervisor), so it answers with the picker rather than a pointer to
# ``/config`` — see ``test_context_picker_offers_supervisor_when_empty``.
_COMMANDS = [
    ("/start", "start_handler"),
    ("/clear", "clear_handler"),
    ("/status", "status_handler"),
    ("/model", "model_handler"),
    ("/effort", "effort_handler"),
    ("/add_dir", "add_dir_handler"),
    ("/resume", "resume_handler"),
    ("/review", "review_handler"),
    ("/vnc", "vnc_handler"),
    ("/phone", "phone_handler"),
    ("/security_key", "security_key_handler"),
]


@pytest.mark.parametrize("text,handler_name", _COMMANDS)
@pytest.mark.asyncio
async def test_command_answers_with_words_when_no_contexts(text, handler_name, db):
    from open_shrimp.handlers import commands

    update = _StubUpdate(text)
    await getattr(commands, handler_name)(update, _StubContext(db))

    assert update.message.replies, f"{text} said nothing at all"
    joined = " ".join(update.message.replies)
    # The route that needs nothing but this chat: the OpenShrimp context
    # writes config.yaml, so a user with no project is never sent back to the
    # machine the bot runs on.
    assert "OpenShrimp" in joined, f"{text} did not point anywhere useful: {joined!r}"
    assert "/context" in joined, f"{text} did not point anywhere useful: {joined!r}"


# ---------------------------------------------------------------------------
# Pick-up picker
# ---------------------------------------------------------------------------


def test_pickup_picker_renders_with_empty_contexts():
    from open_shrimp.events.pickup import _build_picker, _default_context_for

    config = _empty_config()
    default = _default_context_for("lark", config)
    assert default is None

    markup = _build_picker(config, event_id=1, default_ctx=default)
    # Exactly the cancel row survives — no context buttons, nothing empty.
    assert len(markup.inline_keyboard) == 1


@pytest.mark.asyncio
async def test_pickup_open_picker_explains_instead_of_empty_menu(db):
    from open_shrimp.db import insert_inbound_event
    from open_shrimp.events.pickup import _open_picker

    event_id = await insert_inbound_event(
        db,
        source="lark",
        sender="Alice",
        text="hello",
        raw=None,
        chat_id=CHAT_ID,
        thread_id=1,
    )

    answers: list[dict] = []
    edits: list[Any] = []

    class _Query:
        message = type("M", (), {"chat_id": CHAT_ID, "message_thread_id": 1})()

        async def answer(self, text: str | None = None, **kw: Any) -> None:
            answers.append({"text": text, **kw})

        async def edit_message_reply_markup(self, **kw: Any) -> None:
            edits.append(kw)

    ctx = type("C", (), {"bot_data": {"db": db}})()
    await _open_picker(_Query(), str(event_id), _empty_config(), ctx, 0)

    assert answers and answers[0]["text"]
    assert "OpenShrimp" in answers[0]["text"]
    # The "Pick up" button is left in place so the event stays claimable.
    assert not edits


# ---------------------------------------------------------------------------
# Scheduling refuses rather than creating a task that never fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_schedule_refuses_without_a_context(db, monkeypatch):
    from unittest.mock import MagicMock

    from open_shrimp.events import schedule as schedule_mod
    from open_shrimp.tools import create_openshrimp_tools

    class _Runner:
        timezone = "UTC"

    monkeypatch.setattr(schedule_mod, "get_active_runner", lambda: _Runner())

    from open_shrimp.config import EventsConfig

    # Scheduling tools are gated on an events section, not on contexts.
    config = _empty_config()
    config.events = EventsConfig(chat_id=CHAT_ID)

    tools = {
        t.name: t
        for t in create_openshrimp_tools(
            bot=MagicMock(), chat_id=CHAT_ID, db=db, config=config
        )
    }
    result = await tools["create_schedule"].handler({
        "name": "nightly",
        "prompt": "do a thing",
        "schedule_type": "cron",
        "schedule_expr": "0 3 * * *",
    })

    assert "not bound to a project" in result["content"][0]["text"]

    # Nothing was persisted, so there is no task that can never fire.
    from open_shrimp.db import list_scheduled_tasks

    assert await list_scheduled_tasks(db) == []


# ---------------------------------------------------------------------------
# Meetings name their cause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_meeting_notes_names_the_missing_setting():
    from open_shrimp.config import MeetingsConfig
    from open_shrimp.meetings.processor import generate_meeting_notes

    config = _empty_config()
    config.meetings = MeetingsConfig(chat_id=1)

    with pytest.raises(RuntimeError, match="notes_context"):
        await generate_meeting_notes(config, "a transcript")


# ---------------------------------------------------------------------------
# First-boot copy
# ---------------------------------------------------------------------------


def test_the_no_context_copy_points_at_the_supervisor_not_at_the_wizard():
    """No surface may send a user back to the machine the bot runs on: the
    OpenShrimp context writes the config from this chat."""
    from open_shrimp.handlers.utils import no_context_answer, no_context_text

    one_project = _config(
        {"proj": ContextConfig(directory="/tmp", description="Acme", allowed_tools=[])},
        None,
    )
    for config in (_empty_config(), one_project):
        for copy in (no_context_text(config), no_context_answer(config)):
            assert "OpenShrimp" in copy
            assert "/context" in copy
            assert "setup wizard" not in copy

        # Telegram truncates a callback alert past 200 characters, which would
        # cut the remedy off the end of the sentence that carries it.
        assert len(no_context_answer(config)) <= 200


def test_the_no_context_copy_does_not_deny_a_project_that_exists():
    """Setup writes no default_context on purpose, so an install with one
    project reaches its first message unbound.  Saying nothing is set up is
    false there, and sends the user to add a second copy of what they have."""
    from open_shrimp.handlers.utils import no_context_answer, no_context_text

    config = _config(
        {"proj": ContextConfig(directory="/tmp", description="Acme", allowed_tools=[])},
        None,
    )
    for copy in (no_context_text(config), no_context_answer(config)):
        assert "No project is set up yet" not in copy
        assert "picked" in copy

    # The empty install keeps the copy that is true of it.
    assert "No project is set up yet" in no_context_text(_empty_config())


def test_orientation_text_does_not_claim_a_project_when_there_are_none():
    from open_shrimp.first_boot import orientation_text

    text = orientation_text(_empty_config())
    assert "I'm working on" not in text
    assert "No project is set up yet" in text
    assert "OpenShrimp" in text


def test_orientation_text_still_names_the_project_when_there_is_one():
    from open_shrimp.first_boot import orientation_text

    config = _config(
        {"proj": ContextConfig(directory="/tmp", description="Acme", allowed_tools=[])},
        "proj",
    )
    assert "I'm working on Acme." in orientation_text(config)


def test_orientation_text_with_projects_but_no_default_asks_rather_than_denies():
    # Projects exist; only the default is unset.  Saying "no project is set up"
    # here would be false — the copy must point at the picker instead.
    from open_shrimp.first_boot import orientation_text

    config = _config(
        {"proj": ContextConfig(directory="/tmp", description="Acme", allowed_tools=[])},
        None,
    )
    text = orientation_text(config)
    assert "No project is set up yet" not in text
    assert "/context" in text


# ---------------------------------------------------------------------------
# Port-forward label
# ---------------------------------------------------------------------------


def test_port_forward_label_omits_an_absent_context():
    from open_shrimp.port_relay.api import port_forward_label

    config = _empty_config()
    label = port_forward_label(config, None, 8080)
    assert ":8080" in label
    assert "None" not in label


# ---------------------------------------------------------------------------
# Global error handler — the reason a KeyError used to reach the user as silence
# ---------------------------------------------------------------------------


def _error_update(user_id: int, *, callback: bool = False) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    update = MagicMock()
    update.effective_chat.id = CHAT_ID
    update.effective_user.id = user_id
    update.effective_message.message_thread_id = None
    update.callback_query = MagicMock() if callback else None
    if callback:
        update.callback_query.answer = AsyncMock()
    return update


def _error_context(sent: list) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.error = KeyError("ghost")
    ctx.bot.send_message = AsyncMock(side_effect=lambda **kw: sent.append(kw))
    ctx.bot_data = {"config": _config({}, None)}
    ctx.bot_data["config"].allowed_users = [1]
    return ctx


@pytest.mark.asyncio
async def test_error_handler_is_registered_on_the_application():
    from unittest.mock import MagicMock

    from open_shrimp.bot import build_application, handle_error

    app = build_application(_config({}, None), MagicMock(), None)
    assert handle_error in app.error_handlers


@pytest.mark.asyncio
async def test_error_handler_tells_the_affected_chat():
    from open_shrimp.bot import handle_error

    sent: list = []
    await handle_error(_error_update(1), _error_context(sent))
    assert sent and sent[0]["chat_id"] == CHAT_ID
    assert sent[0]["text"].strip()


@pytest.mark.asyncio
async def test_error_handler_stays_silent_for_unauthorized_senders():
    # Answering would confirm a live instance to anyone who can make the bot
    # raise before a handler reaches its own authorization check.
    from open_shrimp.bot import handle_error

    sent: list = []
    await handle_error(_error_update(999), _error_context(sent))
    assert not sent


@pytest.mark.asyncio
async def test_error_handler_answers_a_callback_instead_of_posting():
    from open_shrimp.bot import handle_error

    sent: list = []
    update = _error_update(1, callback=True)
    await handle_error(update, _error_context(sent))
    update.callback_query.answer.assert_awaited()
    assert not sent, "a button press should not also get a stray message"


@pytest.mark.asyncio
async def test_error_handler_swallows_a_delivery_failure():
    from unittest.mock import AsyncMock

    from open_shrimp.bot import handle_error

    sent: list = []
    ctx = _error_context(sent)
    ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    await handle_error(_error_update(1), ctx)  # must not raise
