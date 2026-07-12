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
    claim_inbound_event,
    delete_event_topic,
    get_inbound_event,
    insert_inbound_event,
    prune_inbound_events,
    set_inbound_event_delivery,
)
from open_shrimp.events.pickup import (
    parse_context_directive,
    picked_up_markup,
    pickup_keyboard,
    spawn_pickup_topic,
)
from open_shrimp.events.types import Event
from open_shrimp.markdown import TELEGRAM_MAX_LENGTH, escape, gfm_to_telegram
from open_shrimp.telegram_topics import is_topic_gone, resolve_or_create_topic

logger = logging.getLogger(__name__)

DEDUP_CACHE_SIZE = 512

# Prune runs a DELETE with a subquery, so amortize it over inserts instead
# of paying it on every event.
PRUNE_EVERY = 50

# Forum-topic icon for the per-source inbox topics. Must be one of Telegram's
# allowed topic-icon emoji (📥 is not in that set, so it stays in the title).
INBOX_TOPIC_ICON = "📰"

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
        *,
        context_names: frozenset[str] = frozenset(),
        trusted_senders: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._chat_id = chat_id
        self._pickup_sources = pickup_sources
        # Auto-pickup resolves a /context: directive against these names; the
        # feature is inert unless a source also lists trusted_senders.
        self._context_names = context_names
        self._trusted_senders = trusted_senders or {}
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
        return await resolve_or_create_topic(
            self._bot,
            self._db,
            key=source,
            chat_id=self._chat_id,
            name=f"📥 {source}",
            icon_custom_emoji_id=await self._resolve_icon(),
        )

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
        context_ref = (
            json.dumps(event.context_ref, ensure_ascii=False)
            if event.context_ref is not None
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
            context_ref=context_ref,
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
                if not is_topic_gone(exc):
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

            # A trusted sender's /context: directive auto-picks-up the event —
            # same claim/spawn path as a manual button tap. On any failure it
            # leaves the normal Pick up button in place.
            auto_picked = False
            if (
                event.source in self._pickup_sources
                and message_id is not None
            ):
                auto_picked = await self._maybe_auto_pickup(
                    event, event_id, message_id
                )

            # Acknowledge receipt back to the requester so they know the
            # request landed and is awaiting review. Only for pickup sources:
            # non-pickup events have no operator workflow to be pending on.
            # Skipped when auto-picked: spawn_pickup_topic already sent the
            # picked-up notice, so a receipt would double up.
            if event.source in self._pickup_sources and not auto_picked:
                from open_shrimp.events.progress import (
                    RECEIVED_NOTICE,
                    notify_source,
                )

                await notify_source(event.source, event.reply_ref, RECEIVED_NOTICE)
        except Exception:
            logger.exception(
                "Failed to deliver event from source %r; dropping", event.source
            )

    async def _maybe_auto_pickup(
        self, event: Event, event_id: int, message_id: int
    ) -> bool:
        """Claim + spawn a pick-up topic for a trusted, directive-carrying event.

        Trust is keyed on the platform-stable ``sender_id`` (never the
        display name); the ``/context:`` directive is only honored after that
        check passes and only if it names a defined context. Returns True iff
        the event was claimed and a topic spawned. Never raises.
        """
        trusted = self._trusted_senders.get(event.source, frozenset())
        if event.sender_id is None or event.sender_id not in trusted:
            return False
        ctx_name = parse_context_directive(event.text, self._context_names)
        if ctx_name is None:
            return False
        try:
            # The atomic claim is the race gate against a simultaneous manual tap.
            if not await claim_inbound_event(self._db, event_id):
                return False
            row = await get_inbound_event(self._db, event_id)
            if row is None:
                return False
            outcome = await spawn_pickup_topic(
                self._bot, self._db, row, ctx_name
            )
            if outcome.thread_id is None or outcome.bind_failed:
                return False
            await self._rewrite_pickup_button(
                message_id, outcome.thread_id, ctx_name
            )
            return True
        except Exception:
            logger.exception(
                "Auto-pickup failed for event #%s; leaving it in the inbox",
                event_id,
            )
            return False

    async def _rewrite_pickup_button(
        self, message_id: int, thread_id: int, ctx_name: str
    ) -> None:
        """Swap the inbox Pick up button for the picked-up deep link."""
        try:
            markup = picked_up_markup(
                self._bot.username, self._chat_id, thread_id, ctx_name
            )
            await self._bot.edit_message_reply_markup(
                chat_id=self._chat_id,
                message_id=message_id,
                reply_markup=markup,
            )
        except Exception:
            logger.debug(
                "Failed to rewrite inbox button after auto-pickup", exc_info=True
            )
