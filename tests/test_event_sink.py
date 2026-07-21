"""Tests for the inbound event sink: rendering, topic lifecycle, dedup,
and best-effort delivery."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from open_shrimp.db import get_event_topic, init_db, set_event_topic
from open_shrimp.events import sink as sink_module
from open_shrimp.events.sink import EventSink
from open_shrimp.events.types import Event

CHAT_ID = -1001234


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


def _make_bot(thread_ids: list[int] | None = None) -> AsyncMock:
    bot = AsyncMock()
    ids = iter(thread_ids or [111, 222, 333])
    bot.create_forum_topic.side_effect = lambda *a, **kw: SimpleNamespace(
        message_thread_id=next(ids)
    )
    message_ids = iter(range(1000, 2000))
    bot.send_message.side_effect = lambda *a, **kw: SimpleNamespace(
        message_id=next(message_ids)
    )
    return bot


def _event(**overrides) -> Event:
    fields = {
        "source": "lark",
        "sender": "Alice",
        "text": "hello world",
        "raw": {"k": "v"},
        "dedup_key": None,
    }
    fields.update(overrides)
    return Event(**fields)


# ── Rendering ──


@pytest.mark.asyncio
async def test_text_event_renders_bold_header_with_sender(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(text="hello world"))

    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    assert args[0] == CHAT_ID
    assert args[1].startswith("*📥 lark · Alice*")
    assert "hello world" in args[1]
    assert kwargs["parse_mode"] == "MarkdownV2"
    assert kwargs["message_thread_id"] == 111


@pytest.mark.asyncio
async def test_text_event_without_sender_omits_separator(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(sender=None))

    text = bot.send_message.call_args.args[1]
    assert text.startswith("*📥 lark*")
    assert "·" not in text


@pytest.mark.asyncio
async def test_long_text_is_chunked_into_multiple_messages(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    long_text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(40))
    await sink.emit(_event(text=long_text))

    assert bot.send_message.call_count > 1
    for call in bot.send_message.call_args_list:
        assert len(call.args[1]) <= 4096


@pytest.mark.asyncio
async def test_json_fallback_renders_code_block(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(text=None, raw={"key": "value", "n": 1}))

    text = bot.send_message.call_args.args[1]
    assert text.startswith("*📥 lark · Alice*")
    assert "```json" in text
    assert '"key": "value"' in text
    assert text.endswith("```")


@pytest.mark.asyncio
async def test_json_fallback_truncated_to_single_message(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(text=None, raw={"blob": "y" * 10000}))

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.args[1]
    assert len(text) <= 4096
    assert "… truncated" in text
    # The note must be inside the fenced block.
    assert text.index("… truncated") < text.rindex("```")


# ── Topic lifecycle ──


@pytest.mark.asyncio
async def test_topic_created_on_first_event_and_reused(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(dedup_key="a"))
    await sink.emit(_event(dedup_key="b"))

    bot.create_forum_topic.assert_called_once_with(CHAT_ID, name="📥 lark")
    assert await get_event_topic(db, "lark") == (CHAT_ID, 111)
    for call in bot.send_message.call_args_list:
        assert call.kwargs["message_thread_id"] == 111


@pytest.mark.asyncio
async def test_deleted_topic_recreated_and_send_retried_once(db):
    await set_event_topic(db, "lark", CHAT_ID, 999)  # stale mapping
    bot = _make_bot(thread_ids=[555])
    bot.send_message.side_effect = [
        BadRequest("Message thread not found"),
        None,
    ]
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event())

    assert bot.send_message.call_count == 2
    assert bot.send_message.call_args_list[0].kwargs["message_thread_id"] == 999
    assert bot.send_message.call_args_list[1].kwargs["message_thread_id"] == 555
    assert await get_event_topic(db, "lark") == (CHAT_ID, 555)


@pytest.mark.asyncio
async def test_dead_topic_retry_happens_exactly_once(db):
    await set_event_topic(db, "lark", CHAT_ID, 999)
    bot = _make_bot()
    bot.send_message.side_effect = BadRequest("TOPIC_DELETED")
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event())  # must not raise

    assert bot.send_message.call_count == 2
    assert bot.create_forum_topic.call_count == 1


@pytest.mark.asyncio
async def test_unrelated_bad_request_is_not_retried(db):
    await set_event_topic(db, "lark", CHAT_ID, 999)
    bot = _make_bot()
    bot.send_message.side_effect = BadRequest("Can't parse entities")
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event())

    assert bot.send_message.call_count == 1
    bot.create_forum_topic.assert_not_called()
    assert await get_event_topic(db, "lark") == (CHAT_ID, 999)


# ── Dedup ──


@pytest.mark.asyncio
async def test_duplicate_dedup_key_dropped(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(dedup_key="msg-1"))
    await sink.emit(_event(dedup_key="msg-1"))

    bot.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_is_scoped_per_source(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(source="lark", dedup_key="msg-1"))
    await sink.emit(_event(source="tg-intake", dedup_key="msg-1"))

    assert bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_none_dedup_key_never_deduped(db):
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(dedup_key=None))
    await sink.emit(_event(dedup_key=None))

    assert bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_dedup_lru_eviction(db, monkeypatch):
    monkeypatch.setattr(sink_module, "DEDUP_CACHE_SIZE", 2)
    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(dedup_key="k1"))
    await sink.emit(_event(dedup_key="k2"))
    await sink.emit(_event(dedup_key="k3"))  # evicts k1
    await sink.emit(_event(dedup_key="k1"))  # delivered again

    assert bot.send_message.call_count == 4


# ── Best-effort delivery ──


@pytest.mark.asyncio
async def test_delivery_failure_logged_and_swallowed(db, caplog):
    bot = _make_bot()
    bot.send_message.side_effect = RuntimeError("network down")
    sink = EventSink(bot, db, CHAT_ID)

    with caplog.at_level(logging.ERROR):
        await sink.emit(_event())  # must not raise

    assert any(
        "Failed to deliver event" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_topic_creation_failure_swallowed(db, caplog):
    bot = _make_bot()
    bot.create_forum_topic.side_effect = RuntimeError("no rights")
    sink = EventSink(bot, db, CHAT_ID)

    with caplog.at_level(logging.ERROR):
        await sink.emit(_event())

    bot.send_message.assert_not_called()
    assert any(
        "Failed to deliver event" in record.message for record in caplog.records
    )


def test_sink_never_imports_dispatch_registry():
    # No LLM runs on event receipt.  The docstring may mention the module
    # by name, but no code line may import or call it.
    source = Path(sink_module.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all("dispatch_registry" not in a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "dispatch_registry" not in (node.module or "")
            assert all("dispatch_registry" not in a.name for a in node.names)
        elif isinstance(node, ast.Name):
            assert node.id != "dispatch_registry"


# ── Pick-up button + persistence ──


def _pickup_sink(bot, db, sources=("lark",)) -> EventSink:
    return EventSink(bot, db, CHAT_ID, pickup_sources=frozenset(sources))


@pytest.mark.asyncio
async def test_pickup_button_attached_and_event_persisted(db):
    from open_shrimp.db import get_inbound_event
    from open_shrimp.events.pickup import PICKUP_PREFIX

    bot = _make_bot()
    sink = _pickup_sink(bot, db)

    await sink.emit(_event(text="hello world"))

    markup = bot.send_message.call_args.kwargs["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert button.text == "▶️ Pick up"
    event_id = int(button.callback_data.removeprefix(PICKUP_PREFIX))

    row = await get_inbound_event(db, event_id)
    assert row is not None
    assert row.source == "lark"
    assert row.text == "hello world"
    assert row.chat_id == CHAT_ID
    assert row.thread_id == 111
    assert row.message_id == 1000
    assert row.picked_up is False


@pytest.mark.asyncio
async def test_chunked_event_gets_button_on_last_chunk_only(db):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _pickup_sink(bot, db)

    long_text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(40))
    await sink.emit(_event(text=long_text))

    calls = bot.send_message.call_args_list
    assert len(calls) > 1
    for call in calls[:-1]:
        assert call.kwargs["reply_markup"] is None
    assert calls[-1].kwargs["reply_markup"] is not None

    # The stored message_id is the button host (the last chunk).
    last_message_id = 1000 + len(calls) - 1
    row = await get_inbound_event_by_message(db, CHAT_ID, last_message_id)
    assert row is not None


@pytest.mark.asyncio
async def test_non_pickup_source_gets_no_button_but_is_persisted(db):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = EventSink(bot, db, CHAT_ID)  # no pickup sources

    await sink.emit(_event(text="hello world"))

    assert bot.send_message.call_args.kwargs["reply_markup"] is None
    # Still persisted: the reply path quotes provider content from this row.
    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.text == "hello world"


@pytest.mark.asyncio
async def test_json_fallback_raw_payload_persisted(db):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _pickup_sink(bot, db)

    await sink.emit(_event(text=None, raw={"key": "value"}))

    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.text is None
    assert row.raw == '{"key": "value"}'


@pytest.mark.asyncio
async def test_reply_ref_persisted_as_json(db):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _pickup_sink(bot, db)

    await sink.emit(_event(reply_ref={"message_id": "om_1"}))

    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.reply_ref == '{"message_id": "om_1"}'


@pytest.mark.asyncio
async def test_missing_reply_ref_persisted_as_null(db):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _pickup_sink(bot, db)

    await sink.emit(_event())

    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.reply_ref is None


@pytest.mark.asyncio
async def test_recreated_topic_delivery_records_new_thread(db):
    from open_shrimp.db import get_inbound_event_by_message

    await set_event_topic(db, "lark", CHAT_ID, 999)  # stale mapping
    bot = _make_bot(thread_ids=[555])
    bot.send_message.side_effect = [
        BadRequest("Message thread not found"),
        SimpleNamespace(message_id=1000),
    ]
    sink = _pickup_sink(bot, db)

    await sink.emit(_event())

    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.thread_id == 555


# ── Inbox topic icon ──


@pytest.mark.asyncio
async def test_inbox_topic_created_with_icon_when_available(db):
    from open_shrimp.events.sink import INBOX_TOPIC_ICON

    bot = _make_bot()
    bot.get_forum_topic_icon_stickers.return_value = [
        SimpleNamespace(emoji="🔥", custom_emoji_id="EMOJI_FIRE"),
        SimpleNamespace(emoji=INBOX_TOPIC_ICON, custom_emoji_id="EMOJI_INBOX"),
    ]
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event(source="lark", dedup_key="a"))
    await sink.emit(_event(source="other", dedup_key="b"))  # a second topic

    assert bot.create_forum_topic.call_count == 2
    for call in bot.create_forum_topic.call_args_list:
        assert call.kwargs["icon_custom_emoji_id"] == "EMOJI_INBOX"
    # Icon set is fetched once and cached across topic creations.
    assert bot.get_forum_topic_icon_stickers.call_count == 1


@pytest.mark.asyncio
async def test_topic_created_without_icon_when_set_unavailable(db):
    bot = _make_bot()
    bot.get_forum_topic_icon_stickers.side_effect = RuntimeError("no stickers")
    sink = EventSink(bot, db, CHAT_ID)

    await sink.emit(_event())  # must not raise

    bot.create_forum_topic.assert_called_once_with(CHAT_ID, name="📥 lark")


# ── Trusted-sender auto-pickup ──

TRUSTED_ID = "ou_trusted"
AUTO_CONTEXT = "glints-dockerfiles"


def _auto_sink(bot, db, trusted=(TRUSTED_ID,)) -> EventSink:
    bot.username = "shrimpbot"
    return EventSink(
        bot,
        db,
        CHAT_ID,
        pickup_sources=frozenset({"lark"}),
        get_context_names=lambda: frozenset({"default", AUTO_CONTEXT}),
        trusted_senders={"lark": frozenset(trusted)},
    )


@pytest.fixture
def dispatched(monkeypatch):
    """Capture dispatch() calls made by spawn_pickup_topic."""
    calls: list[dict] = []

    async def fake_dispatch(prompt, chat_id, thread_id=None, *, placeholder=None):
        calls.append({"prompt": prompt, "chat_id": chat_id, "thread_id": thread_id})

    monkeypatch.setattr("open_shrimp.dispatch_registry.dispatch", fake_dispatch)
    return calls


@pytest.fixture
def received(monkeypatch):
    """Capture RECEIVED_NOTICE sends so we can assert they're skipped."""
    notify = AsyncMock(return_value=False)
    monkeypatch.setattr("open_shrimp.events.progress.notify_source", notify)
    return notify


@pytest.mark.asyncio
async def test_trusted_sender_directive_auto_picks_up(db, dispatched, received):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()  # inbox topic -> 111, pick-up topic -> 222
    sink = _auto_sink(bot, db)

    await sink.emit(
        _event(sender_id=TRUSTED_ID, text=f"take a look /context:{AUTO_CONTEXT}")
    )

    # A second topic was spawned and the first turn dispatched into it.
    assert bot.create_forum_topic.call_count == 2
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 222

    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
    assert row.picked_up is True
    assert row.pickup_thread_id == 222

    # The inbox button was rewritten to the picked-up deep link.
    markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text.startswith("✅ Picked up")
    # No receipt notice: the picked-up notice already went out (both notices
    # share notify_source, so assert on the text rather than the call count).
    from open_shrimp.events.progress import RECEIVED_NOTICE

    sent_texts = [call.args[2] for call in received.call_args_list]
    assert RECEIVED_NOTICE not in sent_texts


@pytest.mark.asyncio
async def test_untrusted_sender_directive_not_picked_up(db, dispatched, received):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _auto_sink(bot, db)

    await sink.emit(
        _event(sender_id="ou_stranger", text=f"/context:{AUTO_CONTEXT}")
    )

    assert bot.create_forum_topic.call_count == 1  # inbox only
    assert dispatched == []
    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None and row.picked_up is False
    bot.edit_message_reply_markup.assert_not_called()
    received.assert_called_once()  # normal receipt notice still fires


@pytest.mark.asyncio
async def test_trusted_sender_without_directive_not_picked_up(
    db, dispatched, received
):
    bot = _make_bot()
    sink = _auto_sink(bot, db)

    await sink.emit(_event(sender_id=TRUSTED_ID, text="just a plain message"))

    assert dispatched == []
    bot.edit_message_reply_markup.assert_not_called()
    received.assert_called_once()


@pytest.mark.asyncio
async def test_trusted_sender_unknown_context_not_picked_up(
    db, dispatched, received
):
    bot = _make_bot()
    sink = _auto_sink(bot, db)

    await sink.emit(_event(sender_id=TRUSTED_ID, text="/context:does-not-exist"))

    assert dispatched == []
    bot.edit_message_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_auto_pickup_yields_to_concurrent_manual_claim(
    db, dispatched, received, monkeypatch
):
    from open_shrimp.db import get_inbound_event_by_message

    bot = _make_bot()
    sink = _auto_sink(bot, db)
    # Simulate a human tapping Pick up first: the atomic claim is lost.
    monkeypatch.setattr(
        "open_shrimp.events.sink.claim_inbound_event",
        AsyncMock(return_value=False),
    )

    await sink.emit(
        _event(sender_id=TRUSTED_ID, text=f"/context:{AUTO_CONTEXT}")
    )

    assert dispatched == []
    assert bot.create_forum_topic.call_count == 1  # no pick-up topic
    bot.edit_message_reply_markup.assert_not_called()
    row = await get_inbound_event_by_message(db, CHAT_ID, 1000)
    assert row is not None
