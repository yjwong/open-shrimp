"""Requester-facing progress notifications for inbound events.

Keeps the person who sent an inbound event informed as it moves through
its lifecycle — received (sitting in the inbox), picked up for review, and
pending the operator's response. Notices go back through the source
adapter's reply capability; once a pick-up topic exists they are also
echoed into it so the operator sees exactly what the requester was told.

Best-effort throughout: a failed notification never blocks event handling.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from open_shrimp.db import ChatScope, InboundEvent

logger = logging.getLogger(__name__)

RECEIVED_NOTICE = (
    "📥 Got your request — it's in the queue for review. I'll keep you posted."
)
PICKED_UP_NOTICE = "👀 Your request is now being reviewed."
PENDING_NOTICE = (
    "⏳ Your request has been reviewed and is now pending a response. "
    "I'll follow up as soon as there's an update."
)


async def notify_source(source: str, reply_ref: dict | None, text: str) -> bool:
    """Reply *text* back to an event's origin via its source adapter.

    A no-op (returns ``False``) when the source provided no reply routing
    or isn't currently running a reply-capable adapter. Never raises.
    """
    if reply_ref is None:
        return False

    from open_shrimp.events.base import SupportsReply
    from open_shrimp.events.manager import get_active_adapter

    adapter = get_active_adapter(source)
    if not isinstance(adapter, SupportsReply):
        return False
    try:
        await adapter.reply(reply_ref, text)
        return True
    except Exception:
        logger.warning(
            "Failed to notify requester for source %r", source, exc_info=True,
        )
        return False


async def echo_reply_to_topic(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    header: str,
    sender: str | None,
    body: str,
) -> None:
    """Echo an outbound reply into a forum topic as truncated MarkdownV2.

    Lets the operator see exactly what left for the source without opening
    the source app. Shared by ``reply_inbound_event`` and the lifecycle
    notices below.
    """
    from open_shrimp.markdown import TELEGRAM_MAX_LENGTH, escape

    if sender:
        header = f"{header} · {sender}"
    budget = TELEGRAM_MAX_LENGTH // 2
    if len(body) > budget:
        body = body[:budget] + "…"
    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id
    await bot.send_message(
        chat_id,
        f"*{escape(header)}*\n\n{escape(body)}",
        parse_mode="MarkdownV2",
        **thread_kwargs,
    )


async def notify_requester(
    bot: Any,
    row: InboundEvent,
    text: str,
    *,
    echo_thread_id: int | None = None,
) -> bool:
    """Notify the requester of a persisted event, optionally echoing to a topic.

    Returns ``True`` if the reply reached the source. When *echo_thread_id*
    is given and the reply was sent, a copy is posted into that topic.
    """
    reply_ref = json.loads(row.reply_ref) if row.reply_ref else None
    sent = await notify_source(row.source, reply_ref, text)
    if sent and echo_thread_id is not None:
        try:
            await echo_reply_to_topic(
                bot, row.chat_id, echo_thread_id,
                f"↪️ Told {row.source}", row.sender, text,
            )
        except Exception:
            logger.warning(
                "Failed to echo requester notice for event #%s",
                row.id, exc_info=True,
            )
    return sent


async def notify_pending_if_needed(
    bot: Any, db: aiosqlite.Connection, scope: ChatScope
) -> None:
    """Tell an inbound-event requester their picked-up event is pending review.

    Fires at most once per event (the ``pending_notified`` flag) and only in
    a pick-up topic whose source is reply-capable. Best-effort; never raises.
    """
    if scope.thread_id is None:
        return

    from open_shrimp.events.manager import get_active_manager

    # No running event sources means no pick-up topic exists; skip the DB.
    if get_active_manager() is None:
        return
    try:
        from open_shrimp.db import (
            get_inbound_event_by_pickup_scope,
            mark_inbound_event_pending_notified,
        )

        row = await get_inbound_event_by_pickup_scope(
            db, scope.chat_id, scope.thread_id,
        )
        if row is None or row.reply_ref is None or row.pending_notified:
            return
        if await notify_requester(
            bot, row, PENDING_NOTICE, echo_thread_id=scope.thread_id,
        ):
            await mark_inbound_event_pending_notified(db, row.id)
    except Exception:
        logger.warning(
            "Failed to notify event requester of pending state for scope %s",
            scope, exc_info=True,
        )
