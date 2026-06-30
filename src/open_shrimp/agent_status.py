"""Per-ChatScope agent-status push notifications for the Android companion.

The companion app renders an Android 16 *Live Update* for each active
conversation, modelling the turn lifecycle as three discrete transitions:

- ``started``             — the agent began working on a turn
- ``permission_required`` — a tool needs the user's approval
- ``done``                — the agent went idle

Events are delivered as FCM data messages, one stable notification per
:class:`~open_shrimp.db.ChatScope`.  The permission-required event is sent
high-priority so the OS does not defer the time-sensitive one; it also
carries the ``tool_use_id`` so the notification's inline approve/deny
actions resolve the right server-side future (see
``/api/agent/approvals/{tool_use_id}``).
"""

from __future__ import annotations

import logging
import zlib
from typing import Any, Literal

import aiosqlite

from open_shrimp.android_companion import list_active_android_push_devices
from open_shrimp.android_push import get_push_sender
from open_shrimp.config import Config
from open_shrimp.db import ChatScope

logger = logging.getLogger(__name__)

AgentStatusState = Literal["started", "permission_required", "done"]

_DEFAULT_STATUS_TEXT: dict[str, str] = {"started": "Working…", "done": "Done"}


def scope_notification_id(scope: ChatScope) -> int:
    """Derive a stable, positive notification id from a ChatScope.

    Repeated events for the same conversation reuse this id so the phone
    updates the existing notification rather than stacking new ones, and so
    the ``done`` event dismisses exactly the right one.  Computed with crc32
    so the bot and the phone can agree on the value independently.
    """
    return zlib.crc32(scope.key.encode("utf-8")) & 0x7FFFFFFF


async def notify_agent_status(
    bot_data: Any,
    config: Config,
    db: aiosqlite.Connection,
    scope: ChatScope,
    state: AgentStatusState,
    *,
    title: str,
    text: str | None = None,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Push an agent-status event to every active FCM companion device.

    Best-effort: any failure (no devices, FCM not configured, network) is
    swallowed so the agent turn is never blocked on notification delivery.
    """
    if config.android_companion.push_provider != "fcm":
        return
    if text is None:
        text = _DEFAULT_STATUS_TEXT.get(state, "")
    try:
        devices = await list_active_android_push_devices(db)
    except Exception:
        logger.debug("Failed to list Android push devices", exc_info=True)
        return

    fcm_devices = [d for d in devices if d.get("push_provider") == "fcm"]
    if not fcm_devices:
        return

    sender = get_push_sender(bot_data, config)
    data: dict[str, str] = {
        "type": "agent_status",
        "state": state,
        "scope_key": scope.key,
        "chat_id": str(scope.chat_id),
        "thread_id": "" if scope.thread_id is None else str(scope.thread_id),
        "notification_id": str(scope_notification_id(scope)),
        "title": title,
        "text": text,
    }
    if tool_use_id:
        data["tool_use_id"] = tool_use_id
    if tool_name:
        data["tool_name"] = tool_name

    high_priority = state == "permission_required"
    for device in fcm_devices:
        try:
            await sender.send_agent_status(
                device=device, data=data, high_priority=high_priority,
            )
        except Exception:
            logger.debug(
                "Failed to send agent-status push to device %s",
                device.get("device_id"),
                exc_info=True,
            )
