"""Shared forum-topic helper: gone-detection and resolve-or-create."""

from __future__ import annotations

import asyncio

import pytest
from telegram.error import BadRequest

from open_shrimp.db import get_event_topic, init_db
from open_shrimp.telegram_topics import is_topic_gone, resolve_or_create_topic

CHAT_ID = -1001234


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


@pytest.mark.parametrize(
    "message",
    ["Message thread not found", "TOPIC_DELETED", "the topic deleted by admin"],
)
def test_is_topic_gone_matches_known_markers(message):
    assert is_topic_gone(BadRequest(message))


def test_is_topic_gone_ignores_unrelated_errors():
    assert not is_topic_gone(BadRequest("chat not found"))


class _FakeTopic:
    def __init__(self, thread_id: int) -> None:
        self.message_thread_id = thread_id


class _FakeBot:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_forum_topic(self, chat_id, **kwargs):
        self.created.append({"chat_id": chat_id, **kwargs})
        return _FakeTopic(500 + len(self.created))


@pytest.mark.asyncio
async def test_creates_topic_then_reuses_persisted_thread(db):
    bot = _FakeBot()

    first = await resolve_or_create_topic(
        bot, db, key="meetings", chat_id=CHAT_ID, name="📝 Meetings"
    )
    assert first == 501
    assert await get_event_topic(db, "meetings") == (CHAT_ID, 501)

    # Second call reuses the persisted mapping — no new topic created.
    second = await resolve_or_create_topic(
        bot, db, key="meetings", chat_id=CHAT_ID, name="📝 Meetings"
    )
    assert second == 501
    assert len(bot.created) == 1


@pytest.mark.asyncio
async def test_icon_is_forwarded_only_when_present(db):
    bot = _FakeBot()

    await resolve_or_create_topic(
        bot, db, key="a", chat_id=CHAT_ID, name="A", icon_custom_emoji_id="e1"
    )
    await resolve_or_create_topic(bot, db, key="b", chat_id=CHAT_ID, name="B")

    assert bot.created[0]["icon_custom_emoji_id"] == "e1"
    assert "icon_custom_emoji_id" not in bot.created[1]
