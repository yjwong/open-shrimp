"""Tests for the Telegram intake event source adapter."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.telegram_intake import (
    TelegramIntakeAdapter,
    build_event,
    handle_intake_update,
    media_placeholder,
)


def make_message(
    chat_id: int = 42,
    message_id: int = 7,
    chat_type: str = "private",
    chat_title: str | None = None,
    text: str | None = None,
    caption: str | None = None,
    full_name: str | None = "Alice Smith",
    username: str | None = None,
    user_id: int = 555,
    **media: object,
) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=chat_title)
    user = (
        SimpleNamespace(id=user_id, full_name=full_name, username=username)
        if full_name
        else None
    )
    msg = SimpleNamespace(
        chat=chat,
        message_id=message_id,
        text=text,
        caption=caption,
        from_user=user,
        sender_chat=None,
        **media,
    )
    msg.to_dict = lambda: {"message_id": message_id, "chat": {"id": chat_id}}
    return msg


def make_update(msg: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(effective_message=msg, effective_chat=msg.chat)


def make_source(allowed_chats: list[int] | None = None) -> EventSourceConfig:
    return EventSourceConfig(
        name="tg-intake",
        type="telegram",
        token="123456:TEST",
        allowed_chats=allowed_chats if allowed_chats is not None else [42],
    )


# ---------------------------------------------------------------- ACL


@pytest.mark.asyncio
async def test_allowed_chat_emits() -> None:
    emitted = []

    async def emit(event):
        emitted.append(event)

    msg = make_message(chat_id=42, message_id=7, text="hello")
    await handle_intake_update("tg-intake", {42}, emit, make_update(msg))

    assert len(emitted) == 1
    event = emitted[0]
    assert event.source == "tg-intake"
    assert event.text == "hello"
    assert event.dedup_key == "42:7"
    assert event.raw == {"message_id": 7, "chat": {"id": 42}}


@pytest.mark.asyncio
async def test_disallowed_chat_dropped_and_logged(caplog) -> None:
    emit = AsyncMock()
    msg = make_message(chat_id=999, text="spam")
    with caplog.at_level(logging.INFO, logger="open_shrimp.events.telegram_intake"):
        await handle_intake_update("tg-intake", {42}, emit, make_update(msg))
    emit.assert_not_awaited()
    assert any("dropping" in r.message and "999" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_update_without_message_ignored() -> None:
    emit = AsyncMock()
    update = SimpleNamespace(effective_message=None, effective_chat=None)
    await handle_intake_update("tg-intake", {42}, emit, update)
    emit.assert_not_awaited()


# ---------------------------------------------------------------- text/caption


def test_text_preferred_over_caption() -> None:
    msg = make_message(text="body", caption="cap")
    assert build_event("s", msg).text == "body"


def test_caption_used_when_no_text() -> None:
    msg = make_message(text=None, caption="a photo caption", photo=[object()])
    assert build_event("s", msg).text == "a photo caption"


# ---------------------------------------------------------------- media placeholders


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ({"photo": [object()]}, "[photo]"),
        ({"video": object()}, "[video]"),
        ({"video_note": object()}, "[video note]"),
        ({"voice": object()}, "[voice]"),
        ({"audio": object()}, "[audio]"),
        ({"sticker": SimpleNamespace(emoji="🦐")}, "[sticker 🦐]"),
        ({"sticker": SimpleNamespace(emoji=None)}, "[sticker]"),
        ({"document": SimpleNamespace(file_name="report.pdf")}, "[document: report.pdf]"),
        ({"document": SimpleNamespace(file_name=None)}, "[document]"),
        ({"contact": object()}, "[contact]"),
        ({"location": object()}, "[location]"),
        ({"poll": SimpleNamespace(question="lunch?")}, "[poll: lunch?]"),
    ],
)
def test_media_placeholder(media: dict, expected: str) -> None:
    msg = make_message(text=None, **media)
    assert media_placeholder(msg) == expected
    assert build_event("s", msg).text == expected


def test_animation_wins_over_document() -> None:
    msg = make_message(
        text=None,
        animation=object(),
        document=SimpleNamespace(file_name="anim.mp4"),
    )
    assert media_placeholder(msg) == "[animation]"


def test_unknown_media_falls_back_to_none_with_raw() -> None:
    msg = make_message(text=None)
    event = build_event("s", msg)
    assert event.text is None
    assert event.raw is not None


# ---------------------------------------------------------------- sender formatting


def test_sender_private_no_username() -> None:
    msg = make_message(full_name="Alice Smith", username=None)
    assert build_event("s", msg).sender == "Alice Smith"


def test_sender_private_with_username() -> None:
    msg = make_message(full_name="Alice Smith", username="alice")
    assert build_event("s", msg).sender == "Alice Smith @alice"


def test_sender_group_prefixes_chat_title() -> None:
    msg = make_message(
        chat_type="supergroup",
        chat_title="Foo",
        full_name="Alice",
        username="alice",
    )
    assert build_event("s", msg).sender == "group Foo / Alice @alice"


def test_sender_group_without_user() -> None:
    msg = make_message(chat_type="group", chat_title="Foo", full_name=None)
    assert build_event("s", msg).sender == "group Foo"


# ---------------------------------------------------------------- dedup key


def test_reply_ref_carries_chat_and_message_id() -> None:
    event = build_event("tg-intake", make_message(chat_id=42, message_id=7, text="x"))
    assert event.reply_ref == {"chat_id": 42, "message_id": 7}


@pytest.mark.asyncio
async def test_reply_sends_telegram_reply() -> None:
    adapter = TelegramIntakeAdapter(make_source())
    adapter._app = SimpleNamespace(bot=AsyncMock())

    await adapter.reply({"chat_id": 42, "message_id": 7}, "hello")

    adapter._app.bot.send_message.assert_awaited_once_with(
        42, "hello", reply_to_message_id=7
    )


@pytest.mark.asyncio
async def test_reply_before_start_raises() -> None:
    adapter = TelegramIntakeAdapter(make_source())
    with pytest.raises(RuntimeError):
        await adapter.reply({"chat_id": 42, "message_id": 7}, "hello")


@pytest.mark.asyncio
async def test_reply_with_bad_routing_raises() -> None:
    adapter = TelegramIntakeAdapter(make_source())
    adapter._app = SimpleNamespace(bot=AsyncMock())
    with pytest.raises(ValueError):
        await adapter.reply({"chat_id": "42"}, "hello")
    adapter._app.bot.send_message.assert_not_awaited()


def test_dedup_key_format() -> None:
    msg = make_message(chat_id=-100987, message_id=314)
    assert build_event("s", msg).dedup_key == "-100987:314"


# ---------------------------------------------------------------- lifecycle


def make_mock_app(running: bool = True, updater_running: bool = True) -> MagicMock:
    app = MagicMock()
    app.running = running
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = updater_running
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(monkeypatch) -> None:
    adapter = TelegramIntakeAdapter(make_source())
    app = make_mock_app()
    monkeypatch.setattr(adapter, "_build_application", lambda: app)

    await adapter.start(AsyncMock())
    await adapter._startup_task

    app.initialize.assert_awaited_once()
    app.start.assert_awaited_once()
    app.updater.start_polling.assert_awaited_once()
    app.add_handler.assert_called_once()

    await adapter.stop()

    app.updater.stop.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_after_failed_start_does_not_raise(monkeypatch) -> None:
    adapter = TelegramIntakeAdapter(make_source())

    def boom() -> MagicMock:
        raise RuntimeError("network down")

    monkeypatch.setattr(adapter, "_build_application", boom)

    await adapter.start(AsyncMock())
    await asyncio.sleep(0.05)  # let the first attempt fail and enter backoff
    await adapter.stop()  # must not raise

    assert adapter._startup_task is None
    assert adapter._app is None


@pytest.mark.asyncio
async def test_stop_after_partial_start(monkeypatch) -> None:
    """initialize() succeeded but start_polling() never ran."""
    adapter = TelegramIntakeAdapter(make_source())
    app = make_mock_app(running=False, updater_running=False)
    app.start = AsyncMock(side_effect=RuntimeError("start failed"))
    monkeypatch.setattr(adapter, "_build_application", lambda: app)

    await adapter.start(AsyncMock())
    await asyncio.sleep(0.05)
    await adapter.stop()

    app.updater.stop.assert_not_awaited()
    app.stop.assert_not_awaited()
    app.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_stop_before_any_start() -> None:
    adapter = TelegramIntakeAdapter(make_source())
    await adapter.stop()  # must not raise


@pytest.mark.asyncio
async def test_startup_retries_with_backoff(monkeypatch) -> None:
    adapter = TelegramIntakeAdapter(make_source())
    app = make_mock_app()
    attempts = 0

    def flaky() -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first attempt fails")
        return app

    monkeypatch.setattr(adapter, "_build_application", flaky)
    monkeypatch.setattr("open_shrimp.events.telegram_intake._BACKOFF_INITIAL_S", 0.01)

    await adapter.start(AsyncMock())
    await asyncio.wait_for(adapter._startup_task, timeout=2)

    assert attempts == 2
    app.updater.start_polling.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_handler_routes_to_emit(monkeypatch) -> None:
    adapter = TelegramIntakeAdapter(make_source(allowed_chats=[42]))
    emit = AsyncMock()
    handler = adapter._make_handler(emit)

    msg = make_message(chat_id=42, message_id=1, text="hi")
    await handler(make_update(msg), None)
    emit.assert_awaited_once()

    emit.reset_mock()
    bad = make_message(chat_id=1, message_id=2, text="no")
    await handler(make_update(bad), None)
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_swallows_emit_errors(caplog) -> None:
    adapter = TelegramIntakeAdapter(make_source(allowed_chats=[42]))
    emit = AsyncMock(side_effect=RuntimeError("sink exploded"))
    handler = adapter._make_handler(emit)
    msg = make_message(chat_id=42, text="hi")
    with caplog.at_level(logging.ERROR, logger="open_shrimp.events.telegram_intake"):
        await handler(make_update(msg), None)  # must not raise
    assert any("failed to process" in r.message for r in caplog.records)


# ---------------------------------------------------------------- require_mention

BOT_USERNAME = "yjshouse_ext_bot"
BOT_ID = 8687140347
TARGET = f"@{BOT_USERNAME}"


def _entity(etype, offset, length, user_id=None):
    e = SimpleNamespace(type=etype, offset=offset, length=length)
    if user_id is not None:
        e.user = SimpleNamespace(id=user_id)
    return e


def _mention_msg(text):
    off = text.index(TARGET)
    return make_message(
        chat_id=-100, chat_type="supergroup", text=text,
        entities=[_entity("mention", off, len(TARGET))],
    )


def test_addresses_bot_mention():
    from open_shrimp.events.telegram_intake import addresses_bot
    msg = _mention_msg(f"hey {TARGET} look")
    assert addresses_bot(msg, BOT_USERNAME, BOT_ID) is True


def test_addresses_bot_other_username_ignored():
    from open_shrimp.events.telegram_intake import addresses_bot
    text = "hey @someone_else hi"
    off = text.index("@someone_else")
    msg = make_message(text=text, entities=[_entity("mention", off, len("@someone_else"))])
    assert addresses_bot(msg, BOT_USERNAME, BOT_ID) is False


def test_addresses_bot_command_to_bot():
    from open_shrimp.events.telegram_intake import addresses_bot
    text = f"/status{TARGET}"
    msg = make_message(text=text, entities=[_entity("bot_command", 0, len(text))])
    assert addresses_bot(msg, BOT_USERNAME, BOT_ID) is True


def test_addresses_bot_bare_command_not_addressed():
    from open_shrimp.events.telegram_intake import addresses_bot
    msg = make_message(text="/status", entities=[_entity("bot_command", 0, len("/status"))])
    assert addresses_bot(msg, BOT_USERNAME, BOT_ID) is False


def test_addresses_bot_text_mention_by_id():
    from open_shrimp.events.telegram_intake import addresses_bot
    msg = make_message(text="poke", entities=[_entity("text_mention", 0, 4, user_id=BOT_ID)])
    assert addresses_bot(msg, None, BOT_ID) is True
    other = make_message(text="poke", entities=[_entity("text_mention", 0, 4, user_id=999)])
    assert addresses_bot(other, None, BOT_ID) is False


def test_addresses_bot_caption_mention():
    from open_shrimp.events.telegram_intake import addresses_bot
    cap = f"pic for {TARGET}"
    off = cap.index(TARGET)
    msg = make_message(
        text=None, caption=cap, photo=[object()],
        caption_entities=[_entity("mention", off, len(TARGET))],
    )
    assert addresses_bot(msg, BOT_USERNAME, BOT_ID) is True


def test_addresses_bot_no_entities():
    from open_shrimp.events.telegram_intake import addresses_bot
    assert addresses_bot(make_message(text="plain chatter"), BOT_USERNAME, BOT_ID) is False


@pytest.mark.asyncio
async def test_require_mention_drops_untagged_group_message():
    emit = AsyncMock()
    msg = make_message(chat_id=-100, chat_type="supergroup", text="just chatting")
    await handle_intake_update(
        "s", {-100}, emit, make_update(msg),
        require_mention=True, bot_username=BOT_USERNAME, bot_id=BOT_ID,
    )
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_mention_emits_tagged_group_message():
    emitted = []
    async def emit(e):
        emitted.append(e)
    msg = _mention_msg(f"{TARGET} please look")
    await handle_intake_update(
        "s", {-100}, emit, make_update(msg),
        require_mention=True, bot_username=BOT_USERNAME, bot_id=BOT_ID,
    )
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_require_mention_still_emits_private_dm():
    emitted = []
    async def emit(e):
        emitted.append(e)
    msg = make_message(chat_id=42, chat_type="private", text="hi bot")
    await handle_intake_update(
        "s", {42}, emit, make_update(msg),
        require_mention=True, bot_username=BOT_USERNAME, bot_id=BOT_ID,
    )
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_require_mention_off_emits_everything():
    emit = AsyncMock()
    msg = make_message(chat_id=-100, chat_type="supergroup", text="untagged")
    await handle_intake_update(
        "s", {-100}, emit, make_update(msg),
        require_mention=False, bot_username=BOT_USERNAME, bot_id=BOT_ID,
    )
    emit.assert_awaited_once()
