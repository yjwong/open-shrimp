"""Event sink: render inbound events and post them into per-source topics.

An event is rendered and posted as an inert Telegram message.  The sink
never calls ``dispatch_registry`` — no LLM runs on receipt.  Delivery is
best-effort: on failure the event is logged and dropped, never queued.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict

import aiosqlite
from telegram import Bot
from telegram.error import BadRequest

from open_shrimp.db import (
    delete_event_topic,
    get_event_topic,
    insert_inbound_event,
    prune_inbound_events,
    set_event_topic,
    set_inbound_event_delivery,
)
from open_shrimp.events.pickup import pickup_keyboard
from open_shrimp.events.types import Event
from open_shrimp.markdown import TELEGRAM_MAX_LENGTH, escape, gfm_to_telegram

logger = logging.getLogger(__name__)

DEDUP_CACHE_SIZE = 512

# Prune runs a DELETE with a subquery, so amortize it over inserts instead
# of paying it on every event.
PRUNE_EVERY = 50

# Forum-topic icon for the per-source inbox topics. Must be one of Telegram's
# allowed topic-icon emoji (📥 is not in that set, so it stays in the title).
INBOX_TOPIC_ICON = "📰"

# Telegram reports a deleted/missing forum topic via BadRequest with
# varying descriptions; match broadly on the text.
_TOPIC_GONE_MARKERS = ("message thread not found", "topic_deleted", "topic deleted")


def _is_topic_gone(exc: BadRequest) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TOPIC_GONE_MARKERS)


def _header(event: Event) -> str:
    header = f"📥 {event.source}"
    if event.sender:
        header += f" · {event.sender}"
    # A multi-line sender would break the bold header line.
    return " ".join(header.split())


def _render(event: Event) -> list[str]:
    """Render an event into MarkdownV2 message chunks (each <= 4096 chars)."""
    header = _header(event)
    if event.text is not None:
        return gfm_to_telegram(f"**{header}**\n\n{event.text}")

    bold = f"*{escape(header)}*"
    payload = json.dumps(event.raw, indent=2, ensure_ascii=False)
    budget = TELEGRAM_MAX_LENGTH - len(bold) - len("\n```json\n") - len("\n```")
    if len(payload) > budget:
        note = "\n… truncated"
        payload = payload[: budget - len(note)] + note
    return [f"{bold}\n```json\n{payload}\n```"]


class EventSink:
    """Posts inbound events into per-source forum topics.

    ``emit`` matches :data:`open_shrimp.events.base.EmitFn` and is the entry
    point handed to source adapters.  It never raises.
    """

    def __init__(
        self,
        bot: Bot,
        db: aiosqlite.Connection,
        chat_id: int,
        pickup_sources: frozenset[str] = frozenset(),
    ) -> None:
        self._bot = bot
        self._db = db
        self._chat_id = chat_id
        self._pickup_sources = pickup_sources
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._icon_id: str | None = None
        self._icon_resolved = False

    def _is_duplicate(self, event: Event) -> bool:
        if event.dedup_key is None:
            return False
        key = (event.source, event.dedup_key)
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        while len(self._seen) > DEDUP_CACHE_SIZE:
            self._seen.popitem(last=False)
        return False

    async def _resolve_icon(self) -> str | None:
        """The custom_emoji_id for INBOX_TOPIC_ICON, resolved once and cached.

        Best-effort: returns None (topic created without a custom icon) if the
        icon set can't be fetched or lacks the emoji.  Never raises.
        """
        if self._icon_resolved:
            return self._icon_id
        self._icon_resolved = True
        try:
            stickers = await self._bot.get_forum_topic_icon_stickers()
            for sticker in stickers:
                if sticker.emoji == INBOX_TOPIC_ICON and sticker.custom_emoji_id:
                    self._icon_id = sticker.custom_emoji_id
                    break
        except Exception:
            logger.debug("Could not resolve inbox topic icon", exc_info=True)
        return self._icon_id

    async def _resolve_topic(self, source: str) -> int:
        row = await get_event_topic(self._db, source)
        if row is not None:
            return row[1]
        kwargs: dict[str, str] = {"name": f"📥 {source}"}
        icon_id = await self._resolve_icon()
        if icon_id is not None:
            kwargs["icon_custom_emoji_id"] = icon_id
        topic = await self._bot.create_forum_topic(self._chat_id, **kwargs)
        thread_id: int = topic.message_thread_id
        await set_event_topic(self._db, source, self._chat_id, thread_id)
        logger.info("Created event topic %r (thread_id=%s)", source, thread_id)
        return thread_id

    async def _persist(self, event: Event, thread_id: int) -> int:
        """Persist the provider-delivered event content; returns the row id.

        The stored row is the only permitted source of untrusted content in
        agent prompts (pick-up briefs and reply quoting both read it back).
        """
        raw = (
            json.dumps(event.raw, ensure_ascii=False)
            if event.raw is not None
            else None
        )
        reply_ref = (
            json.dumps(event.reply_ref, ensure_ascii=False)
            if event.reply_ref is not None
            else None
        )
        return await insert_inbound_event(
            self._db,
            source=event.source,
            sender=event.sender,
            text=event.text,
            raw=raw,
            chat_id=self._chat_id,
            thread_id=thread_id,
            reply_ref=reply_ref,
        )

    async def _send_chunks(
        self, chunks: list[str], thread_id: int, event_id: int | None
    ) -> int | None:
        """Send chunks, attaching the pick-up button to the last one only.

        Returns the message_id of the last sent message (the button host).
        """
        message = None
        for i, chunk in enumerate(chunks):
            markup = None
            if event_id is not None and i == len(chunks) - 1:
                markup = pickup_keyboard(event_id)
            message = await self._bot.send_message(
                self._chat_id,
                chunk,
                message_thread_id=thread_id,
                parse_mode="MarkdownV2",
                reply_markup=markup,
            )
        return getattr(message, "message_id", None)

    async def emit(self, event: Event) -> None:
        """Render and deliver an event.  Never raises."""
        try:
            if self._is_duplicate(event):
                logger.debug(
                    "Dropping duplicate event %r from %r",
                    event.dedup_key,
                    event.source,
                )
                return
            chunks = _render(event)
            thread_id = await self._resolve_topic(event.source)
            event_id = await self._persist(event, thread_id)
            button_id = (
                event_id if event.source in self._pickup_sources else None
            )
            try:
                message_id = await self._send_chunks(chunks, thread_id, button_id)
            except BadRequest as exc:
                if not _is_topic_gone(exc):
                    raise
                # The topic was deleted out from under us: recreate and
                # retry exactly once.
                logger.info(
                    "Event topic for %r is gone (%s); recreating", event.source, exc
                )
                await delete_event_topic(self._db, event.source)
                thread_id = await self._resolve_topic(event.source)
                message_id = await self._send_chunks(chunks, thread_id, button_id)
            if message_id is not None:
                await set_inbound_event_delivery(
                    self._db, event_id, thread_id, message_id
                )
                if event_id % PRUNE_EVERY == 0:
                    await prune_inbound_events(self._db, event.source)
            # Acknowledge receipt back to the requester so they know the
            # request landed and is awaiting review. Only for pickup sources:
            # non-pickup events have no operator workflow to be pending on.
            if event.source in self._pickup_sources:
                from open_shrimp.events.progress import (
                    RECEIVED_NOTICE,
                    notify_source,
                )

                await notify_source(event.source, event.reply_ref, RECEIVED_NOTICE)
        except Exception:
            logger.exception(
                "Failed to deliver event from source %r; dropping", event.source
            )
