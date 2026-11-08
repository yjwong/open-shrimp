"""Shared forum-topic helpers.

Both the inbound-event sink and the meetings processor post into dedicated
forum topics recorded in the ``event_topics`` table.  This owns the
create-or-reuse lookup and the "topic was deleted out from under us"
detection so the two callers cannot drift, and the "can this chat host a new
topic" gate shared by the new-topic handoff and ``/fork``.
"""

from __future__ import annotations

import logging

import aiosqlite
from telegram import Bot
from telegram.error import BadRequest

from open_shrimp.db import get_event_topic, set_event_topic

logger = logging.getLogger(__name__)

# Telegram reports a deleted/missing forum topic via BadRequest with varying
# descriptions; match broadly on the text.
_TOPIC_GONE_MARKERS = ("message thread not found", "topic_deleted", "topic deleted")


def is_topic_gone(exc: BadRequest) -> bool:
    """Whether *exc* means the target forum topic no longer exists."""
    message = str(exc).lower()
    return any(marker in message for marker in _TOPIC_GONE_MARKERS)


async def can_create_topics(
    bot: Bot,
    chat_id: int,
    *,
    is_private_chat: bool,
    thread_id: int | None,
) -> bool:
    """Whether ``createForumTopic`` can work in this chat.

    Bots may always create topics in a private chat, but Telegram only sets
    ``Chat.is_forum`` on supergroups, so a private chat reads falsy there and
    is gated on *is_private_chat* instead.  When ``get_chat`` fails, being in a
    topic already (*thread_id* set) implies a forum.
    """
    if is_private_chat:
        return True
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        logger.debug("get_chat failed during topic gate", exc_info=True)
        return thread_id is not None
    return bool(getattr(chat, "is_forum", False))


async def resolve_or_create_topic(
    bot: Bot,
    db: aiosqlite.Connection,
    *,
    key: str,
    chat_id: int,
    name: str,
    icon_custom_emoji_id: str | None = None,
) -> int:
    """Return the message_thread_id for *key*, creating the topic if needed."""
    row = await get_event_topic(db, key)
    if row is not None:
        return row[1]
    kwargs: dict[str, str] = {"name": name}
    if icon_custom_emoji_id is not None:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    topic = await bot.create_forum_topic(chat_id, **kwargs)
    thread_id: int = topic.message_thread_id
    await set_event_topic(db, key, chat_id, thread_id)
    logger.info("Created forum topic %r (thread_id=%s)", key, thread_id)
    return thread_id
