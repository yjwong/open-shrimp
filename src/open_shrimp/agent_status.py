"""Per-ChatScope agent-status push notifications for the Android companion.

The companion app renders an Android 16 *Live Update* for each active
conversation.  The turn lifecycle is just two phases:

- ``running`` — the agent is working on a turn (the notification is live)
- ``done``    — the agent went idle (the notification is dismissed)

Progress is driven by the agent's TodoWrite list: ``running`` events carry
``todo_done``/``todo_total`` counts that render as a segmented bar and an
"x/y" chip, and the ``text`` body reflects the active todo's label (the
``in_progress`` item's ``activeForm``) instead of a generic "Working…".
Awaiting a tool approval is *not* a phase — it is an overlay on
the running notification (``awaiting=1``), adding inline approve/deny actions
and bumping the push to high priority.  The overlay carries the
``tool_use_id`` so those actions resolve the right server-side future (see
``/api/agent/approvals/{tool_use_id}``).

Events are delivered as FCM data messages, one stable notification per
:class:`~open_shrimp.db.ChatScope`.  See the v2 contract in
``AgentStatusNotifier.kt`` for the full field set and rendering rules.
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

AgentStatusPhase = Literal["running", "done"]

_DEFAULT_STATUS_TEXT: dict[str, str] = {"running": "Working…", "done": "Done"}


def current_todo_text(todos: list[dict[str, Any]] | None) -> str | None:
    """Return a label for the todo the agent is actively working on.

    Prefers the ``in_progress`` item's ``activeForm`` ("Running tests") so the
    notification body reads as a live status line; falls back to the first
    not-yet-finished item, and to ``content`` when ``activeForm`` is absent.
    Returns ``None`` when nothing is actionable, so the caller keeps the
    default "Working…" text.
    """
    if not todos:
        return None
    active = next((t for t in todos if t.get("status") == "in_progress"), None)
    if active is None:
        active = next(
            (
                t
                for t in todos
                if t.get("status") not in ("completed", "cancelled")
            ),
            None,
        )
    if active is None:
        return None
    return active.get("activeForm") or active.get("content") or None


def todo_counts(todos: list[dict[str, Any]] | None) -> tuple[int, int] | None:
    """Reduce a TodoWrite list to ``(done, total)`` for the progress bar.

    Returns ``None`` when there are no todos, so the phone falls back to the
    indeterminate "Working…" bar.  Progress is modelled as ``done/total``
    regardless of completion order: the marker may jump if items finish out
    of order, but the count is never misreported.
    """
    if not todos:
        return None
    total = len(todos)
    done = sum(1 for t in todos if t.get("status") == "completed")
    return done, total


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
    phase: AgentStatusPhase,
    *,
    title: str,
    text: str | None = None,
    awaiting: bool = False,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> None:
    """Push an agent-status event to every active FCM companion device.

    ``phase`` is ``running`` or ``done``.  ``awaiting`` overlays an approval
    request on a running notification (it carries ``tool_use_id`` and bumps
    the push to high priority).  ``todos`` is the latest TodoWrite list, used
    to attach ``done/total`` progress counts on running events.

    Best-effort: any failure (no devices, FCM not configured, network) is
    swallowed so the agent turn is never blocked on notification delivery.
    """
    if config.android_companion.push_provider != "fcm":
        return
    if text is None:
        if phase == "running":
            text = current_todo_text(todos) or _DEFAULT_STATUS_TEXT["running"]
        else:
            text = _DEFAULT_STATUS_TEXT.get(phase, "")
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
        "phase": phase,
        "scope_key": scope.key,
        "chat_id": str(scope.chat_id),
        "thread_id": "" if scope.thread_id is None else str(scope.thread_id),
        "notification_id": str(scope_notification_id(scope)),
        "title": title,
        "text": text,
    }
    # Progress counts ride only on running events (a done event dismisses).
    if phase == "running":
        counts = todo_counts(todos)
        if counts is not None:
            done, total = counts
            data["todo_done"] = str(done)
            data["todo_total"] = str(total)
    if awaiting:
        data["awaiting"] = "1"
        if tool_use_id:
            data["tool_use_id"] = tool_use_id
        if tool_name:
            data["tool_name"] = tool_name

    high_priority = awaiting
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
