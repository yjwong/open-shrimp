"""Tests for inbound_events persistence: insert/get, delivery recording,
the atomic claim race gate, release, and pruning."""

from __future__ import annotations

import asyncio

import pytest

from open_shrimp.db import (
    claim_inbound_event,
    get_inbound_event,
    get_inbound_event_by_message,
    get_inbound_event_by_pickup_scope,
    init_db,
    insert_inbound_event,
    prune_inbound_events,
    release_inbound_event,
    set_inbound_event_delivery,
    set_inbound_event_pickup_thread,
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


@pytest.mark.asyncio
async def test_reply_ref_roundtrip(db):
    event_id = await _insert(db, reply_ref='{"message_id": "om_1"}')

    row = await get_inbound_event(db, event_id)
    assert row.reply_ref == '{"message_id": "om_1"}'


@pytest.mark.asyncio
async def test_reply_ref_defaults_to_none(db):
    event_id = await _insert(db)

    row = await get_inbound_event(db, event_id)
    assert row.reply_ref is None
    assert row.pickup_thread_id is None


@pytest.mark.asyncio
async def test_pickup_thread_binding_and_scope_lookup(db):
    event_id = await _insert(db)

    await set_inbound_event_pickup_thread(db, event_id, 777)

    row = await get_inbound_event(db, event_id)
    assert row.pickup_thread_id == 777
    found = await get_inbound_event_by_pickup_scope(db, CHAT_ID, 777)
    assert found is not None
    assert found.id == event_id


@pytest.mark.asyncio
async def test_migration_adds_reply_columns_to_old_table(tmp_path):
    import aiosqlite

    path = tmp_path / "old.sqlite3"
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE inbound_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
        "sender TEXT, text TEXT, raw TEXT, chat_id INTEGER NOT NULL, "
        "thread_id INTEGER NOT NULL, message_id INTEGER, "
        "picked_up INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"
    )
    await conn.execute(
        "INSERT INTO inbound_events "
        "(source, sender, text, raw, chat_id, thread_id, created_at) "
        "VALUES ('lark', 'Alice', 'hi', NULL, 1, 2, 3)"
    )
    await conn.commit()
    await conn.close()

    db = await init_db(path)
    try:
        row = await get_inbound_event(db, 1)
        assert row is not None
        assert row.reply_ref is None
        assert row.pickup_thread_id is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pickup_scope_lookup_misses(db):
    event_id = await _insert(db)
    await set_inbound_event_pickup_thread(db, event_id, 777)

    # Wrong thread, wrong chat, and the inbox thread itself all miss.
    assert await get_inbound_event_by_pickup_scope(db, CHAT_ID, 778) is None
    assert await get_inbound_event_by_pickup_scope(db, 999, 777) is None
    assert await get_inbound_event_by_pickup_scope(db, CHAT_ID, 111) is None
