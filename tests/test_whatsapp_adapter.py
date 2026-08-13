"""Tests for the WhatsApp event source adapter."""

import pytest

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.base import SupportsIngest
from open_shrimp.events.whatsapp import (
    MAX_TEXT_CHARS,
    WhatsAppAdapter,
    build_event,
    format_sender,
    message_text,
    resolve_sender_id,
    should_ingest,
)


def make_row(**overrides: object) -> dict:
    row = {
        "id": 1001,
        "key_id": "3EB0ABC",
        "from_me": 0,
        "timestamp": 1_770_000_000_000,
        "message_type": 0,
        "text": "hello",
        "chat_jid": "6591234567@s.whatsapp.net",
        "chat_subject": None,
        "sender_jid": None,
        "sender_name": "Alice",
    }
    row.update(overrides)
    return row


def make_source(name: str = "whatsapp") -> EventSourceConfig:
    return EventSourceConfig(name=name, type="whatsapp")


# --- sender identity -------------------------------------------------------


@pytest.mark.parametrize(
    "sender_jid",
    ["6598887777@s.whatsapp.net", "123456789012345@lid"],
)
def test_sender_id_prefers_the_row_sender(sender_jid):
    """Either form is a stable id — most LIDs resolve, but an unresolved one
    is still usable in trusted_senders."""
    assert resolve_sender_id(make_row(sender_jid=sender_jid)) == sender_jid


def test_direct_chat_without_a_row_sender_falls_back_to_the_chat():
    """In 1:1 chats the counterparty is the chat itself."""
    assert resolve_sender_id(make_row(sender_jid=None)) == "6591234567@s.whatsapp.net"


def test_group_row_without_a_sender_fails_closed():
    assert resolve_sender_id(make_row(sender_jid=None, chat_jid="12345-67890@g.us")) is None


def test_display_name_is_never_a_sender_id_fallback():
    row = make_row(sender_jid=None, chat_jid="", sender_name="Mallory")
    assert resolve_sender_id(row) is None


def test_format_sender_qualifies_group_messages():
    row = make_row(
        chat_jid="12345-67890@g.us",
        chat_subject="Book Club",
        sender_jid="6598887777@s.whatsapp.net",
        sender_name="Bob",
    )
    assert format_sender(row) == "group Book Club / Bob"


def test_display_name_does_not_inherit_the_trust_policy():
    """format_sender must not go through resolve_sender_id: a group row with
    no sender still has something to display, even though it has no trusted id."""
    row = make_row(sender_jid=None, chat_jid="12345-67890@g.us", sender_name=None)
    assert resolve_sender_id(row) is None
    assert format_sender(row) == "12345-67890@g.us"


def test_format_sender_falls_back_to_the_id_without_a_name():
    row = make_row(sender_name=None, sender_jid="6598887777@s.whatsapp.net")
    assert format_sender(row) == "6598887777@s.whatsapp.net"


# --- body rendering --------------------------------------------------------


def test_text_message_uses_its_body():
    assert message_text(make_row()) == "hello"


def test_media_rows_render_as_placeholders():
    assert message_text(make_row(message_type=1, text=None)) == "[image]"


def test_unknown_media_type_renders_generically():
    """message_text is total: should_ingest is what fails closed."""
    assert message_text(make_row(message_type=118, text=None)) == "[media]"


def test_document_placeholder_carries_the_filename():
    row = make_row(
        message_type=9, text=None, file_path="Media/Documents/q3-report.pdf"
    )
    assert message_text(row) == "[document: q3-report.pdf]"


def test_media_caption_is_appended_to_the_placeholder():
    row = make_row(message_type=1, text=None, caption="  at the beach  ")
    assert message_text(row) == "[image] at the beach"


def test_empty_text_row_yields_no_body():
    assert message_text(make_row(text="")) is None
    assert message_text(make_row(text="   ")) is None


def test_oversized_text_is_capped_in_both_the_body_and_the_stored_row():
    """The sink persists Event.raw verbatim, so capping only the rendered
    body would leave storage unbounded."""
    event = build_event("whatsapp", make_row(text="x" * (MAX_TEXT_CHARS + 500)))
    assert len(event.text) == MAX_TEXT_CHARS
    assert len(event.raw["text"]) == MAX_TEXT_CHARS


def test_small_rows_are_not_copied():
    row = make_row()
    assert build_event("whatsapp", row).raw is row


# --- ingest filter ---------------------------------------------------------


def test_own_outgoing_messages_are_dropped():
    assert not should_ingest(make_row(from_me=1))


def test_system_messages_are_dropped():
    """Type 7 is 5% of the database and is entirely noise."""
    assert not should_ingest(make_row(message_type=7))


def test_revoked_and_unknown_types_fail_closed():
    assert not should_ingest(make_row(message_type=15))
    assert not should_ingest(make_row(message_type=118))
    assert not should_ingest(make_row(message_type="junk"))


def test_rows_without_identity_are_dropped():
    assert not should_ingest(make_row(key_id=""))
    assert not should_ingest(make_row(chat_jid=None))


def test_ordinary_text_row_is_ingested():
    assert should_ingest(make_row())


# --- event mapping ---------------------------------------------------------


def test_build_event_maps_the_contract_fields():
    row = make_row(sender_jid="6598887777@s.whatsapp.net")
    event = build_event("whatsapp", row)
    assert event.source == "whatsapp"
    assert event.text == "hello"
    assert event.sender_id == "6598887777@s.whatsapp.net"
    assert event.dedup_key == "6591234567@s.whatsapp.net:3EB0ABC"
    assert event.raw is row


def test_build_event_carries_no_routing_refs():
    """Read-only: no reply route, and both refs surface to the agent as
    trusted metadata via routing_summary, so neither may be faked."""
    event = build_event("whatsapp", make_row())
    assert event.reply_ref is None
    assert event.context_ref is None


# --- adapter lifecycle and ingest -----------------------------------------


def test_adapter_advertises_the_ingest_capability():
    """The upload endpoint narrows to SupportsIngest rather than to the class."""
    assert isinstance(WhatsAppAdapter(make_source()), SupportsIngest)


@pytest.mark.asyncio
async def test_stop_makes_the_adapter_inert():
    adapter = WhatsAppAdapter(make_source())

    async def emit(event):
        pass

    await adapter.start(emit)
    await adapter.stop()
    with pytest.raises(RuntimeError):
        await adapter.ingest([make_row()])


@pytest.mark.asyncio
async def test_ingest_emits_in_id_order_and_returns_the_watermark():
    seen = []

    async def emit(event):
        seen.append(event)

    adapter = WhatsAppAdapter(make_source())
    await adapter.start(emit)
    try:
        cursor = await adapter.ingest(
            [
                make_row(id=1003, key_id="C", text="third"),
                make_row(id=1001, key_id="A", text="first"),
                make_row(id=1002, key_id="B", text="second"),
            ]
        )
    finally:
        await adapter.stop()

    assert [e.text for e in seen] == ["first", "second", "third"]
    assert cursor == 1003


@pytest.mark.asyncio
async def test_filtered_rows_still_advance_the_cursor():
    """Refusing to acknowledge a dropped row would stall the phone on it."""
    seen = []

    async def emit(event):
        seen.append(event)

    adapter = WhatsAppAdapter(make_source())
    await adapter.start(emit)
    try:
        cursor = await adapter.ingest(
            [
                make_row(id=2001, message_type=7),
                make_row(id=2002, from_me=1),
                make_row(id=2003, message_type=15),
            ]
        )
    finally:
        await adapter.stop()

    assert seen == []
    assert cursor == 2003


@pytest.mark.asyncio
async def test_a_failed_emit_stops_the_batch_before_the_bad_row():
    seen = []

    async def emit(event):
        if event.text == "boom":
            raise RuntimeError("sink is down")
        seen.append(event)

    adapter = WhatsAppAdapter(make_source())
    await adapter.start(emit)
    try:
        cursor = await adapter.ingest(
            [
                make_row(id=3001, key_id="A", text="fine"),
                make_row(id=3002, key_id="B", text="boom"),
                make_row(id=3003, key_id="C", text="later"),
            ]
        )
    finally:
        await adapter.stop()

    assert [e.text for e in seen] == ["fine"]
    assert cursor == 3001


@pytest.mark.asyncio
async def test_ingest_before_start_is_an_error():
    adapter = WhatsAppAdapter(make_source())
    with pytest.raises(RuntimeError):
        await adapter.ingest([make_row()])
