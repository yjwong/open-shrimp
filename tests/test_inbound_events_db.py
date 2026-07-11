"""Tests for inbound_events persistence: insert/get, delivery recording,
the atomic claim race gate, release, and pruning."""

from __future__ import annotations

import asyncio

import pytest

from open_shrimp.db import (
    claim_inbound_event,
    get_inbound_event,
    get_inbound_event_by_message,
    init_db,
    insert_inbound_event,
    prune_inbound_events,
    release_inbound_event,
    set_inbound_event_delivery,
)

CHAT_ID = -1001234


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


async def _insert(db, **overrides) -> int:
    fields = {
        "source": "lark",
        "sender": "Alice",
        "text": "hello",
        "raw": '{"k": "v"}',
        "chat_id": CHAT_ID,
        "thread_id": 111,
    }
    fields.update(overrides)
    return await insert_inbound_event(db, **fields)


@pytest.mark.asyncio
async def test_insert_and_get_roundtrip(db):
    event_id = await _insert(db)

    row = await get_inbound_event(db, event_id)
    assert row is not None
    assert row.id == event_id
    assert row.source == "lark"
    assert row.sender == "Alice"
    assert row.text == "hello"
    assert row.raw == '{"k": "v"}'
    assert row.chat_id == CHAT_ID
    assert row.thread_id == 111
    assert row.message_id is None
    assert row.picked_up is False
    assert row.created_at > 0


@pytest.mark.asyncio
async def test_get_missing_returns_none(db):
    assert await get_inbound_event(db, 12345) is None


@pytest.mark.asyncio
async def test_delivery_recorded(db):
    event_id = await _insert(db)

    await set_inbound_event_delivery(db, event_id, 222, 9001)

    row = await get_inbound_event(db, event_id)
    assert row.thread_id == 222
    assert row.message_id == 9001


@pytest.mark.asyncio
async def test_lookup_by_message(db):
    event_id = await _insert(db)
    await set_inbound_event_delivery(db, event_id, 111, 9001)

    row = await get_inbound_event_by_message(db, CHAT_ID, 9001)
    assert row is not None and row.id == event_id
    assert await get_inbound_event_by_message(db, CHAT_ID, 4444) is None
    assert await get_inbound_event_by_message(db, 999, 9001) is None


@pytest.mark.asyncio
async def test_claim_wins_once(db):
    event_id = await _insert(db)

    assert await claim_inbound_event(db, event_id) is True
    # Second claim (double-tap / concurrent) loses.
    assert await claim_inbound_event(db, event_id) is False
    row = await get_inbound_event(db, event_id)
    assert row.picked_up is True


@pytest.mark.asyncio
async def test_release_makes_event_claimable_again(db):
    event_id = await _insert(db)
    assert await claim_inbound_event(db, event_id) is True

    await release_inbound_event(db, event_id)

    assert await claim_inbound_event(db, event_id) is True


@pytest.mark.asyncio
async def test_prune_keeps_newest_per_source(db):
    ids = [await _insert(db, text=f"e{i}") for i in range(5)]
    other = await _insert(db, source="tg-intake")

    await prune_inbound_events(db, "lark", keep=2)

    for old_id in ids[:3]:
        assert await get_inbound_event(db, old_id) is None
    for kept_id in ids[3:]:
        assert await get_inbound_event(db, kept_id) is not None
    # Other sources untouched.
    assert await get_inbound_event(db, other) is not None
