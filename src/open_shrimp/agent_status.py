"""Per-ChatScope agent-status push notifications for the Android companion.

The companion app renders an Android 16 *Live Update* for each active
conversation.  The turn lifecycle is just two phases:

- ``running`` — the agent is working on a turn (the notification is live)
- ``done``    — the agent went idle (the notification is dismissed)

Progress is driven by the agent's task checklist: ``running`` events carry
``todo_done``/``todo_total`` counts that render as a segmented bar and an
"x/y" chip, and the ``text`` body reflects the active todo's label (the
``in_progress`` item's ``activeForm``) instead of a generic "Working…".

Waiting on the human is *not* a phase — it is an overlay on the running
notification that bumps the push to high priority.  ``awaiting_kind`` names
the wait and is the only field that says one is happening; ``awaiting_id``
answers it, and the endpoint follows from the kind:

- ``approval`` (+ ``tool_name``) — a tool approval, answered by the
  notification's approve/deny actions via ``/api/agent/approvals/{id}``.
- ``question`` (+ ``question_options``, ``multi_select``) — an
  AskUserQuestion, answered by index via ``/api/agent/questions/{id}``.
  The options ride in the push so the phone renders the whole choice
  without calling back, and the answer is a list of positions in that
  same list.

Events are delivered as FCM data messages, one stable notification per
:class:`~open_shrimp.db.ChatScope`.  See the v2 contract in
``AgentStatusNotifier.kt`` for the full field set and rendering rules.
"""

from __future__ import annotations

import json
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

# What the turn is parked on.  The phone derives the endpoint that ends the
# wait from this, so a new kind here is a new route in ``agent_status_api``.
AwaitingKind = Literal["approval", "question"]

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
    """Reduce a task checklist to ``(done, total)`` for the progress bar.

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


# FCM rejects a data message whose keys and values exceed 4KB in total.  The
# rest of an agent-status payload (title, body, deep-link fields) runs to a few
# hundred bytes, so the encoded option list gets this slice of the budget.
_MAX_OPTIONS_BYTES = 2048
_MAX_LABEL_CHARS = 60
_MAX_DESCRIPTION_CHARS = 140
_TRUNCATED_LABEL_CHARS = 24


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def encode_question_options(options: list[dict[str, Any]]) -> str:
    """Serialise an AskUserQuestion option list for the FCM data payload.

    The phone answers with positions in this list, so every option survives
    encoding and the order is the contract.  Only the text shrinks when the
    payload runs past ``_MAX_OPTIONS_BYTES``: descriptions go first, then
    labels are clipped hard.  A list too long to fit even then is sent
    oversized and FCM refuses it, costing the notification — which beats one
    the user answers without having seen the option they wanted.
    """
    encoded = [
        {
            "label": _clip(o.get("label"), _MAX_LABEL_CHARS),
            "description": _clip(o.get("description"), _MAX_DESCRIPTION_CHARS),
        }
        for o in options
    ]
    payload = json.dumps(encoded, separators=(",", ":"))
    if len(payload.encode("utf-8")) <= _MAX_OPTIONS_BYTES:
        return payload

    for entry in encoded:
        entry["description"] = ""
    payload = json.dumps(encoded, separators=(",", ":"))
    if len(payload.encode("utf-8")) <= _MAX_OPTIONS_BYTES:
        return payload

    for entry in encoded:
        entry["label"] = _clip(entry["label"], _TRUNCATED_LABEL_CHARS)
    return json.dumps(encoded, separators=(",", ":"))


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
    awaiting_kind: AwaitingKind | None = None,
    awaiting_id: str | None = None,
    tool_name: str | None = None,
    question_options: list[dict[str, Any]] | None = None,
    multi_select: bool = False,
    todos: list[dict[str, Any]] | None = None,
) -> None:
    """Push an agent-status event to every active FCM companion device.

    ``phase`` is ``running`` or ``done``.  ``awaiting_kind`` overlays a wait
    for the human on a running notification and bumps the push to high
    priority; ``awaiting_id`` is what the phone sends back to end the wait.
    ``todos`` is the latest task checklist, used to attach ``done/total``
    progress counts on running events.

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
        # Lets the phone deep-link the notification tap into the Telegram chat:
        # forum topics/supergroups resolve from chat_id+thread_id; private chats
        # need the bot username (empty when get_me hasn't been cached yet).
        "bot_username": str(bot_data.get("bot_username") or ""),
    }
    # Progress counts ride only on running events (a done event dismisses).
    if phase == "running":
        counts = todo_counts(todos)
        if counts is not None:
            done, total = counts
            data["todo_done"] = str(done)
            data["todo_total"] = str(total)
    if awaiting_kind is not None:
        data["awaiting_kind"] = awaiting_kind
        data["awaiting_id"] = awaiting_id or ""
        if tool_name:
            data["tool_name"] = tool_name
        if awaiting_kind == "question":
            data["question_options"] = encode_question_options(
                question_options or [],
            )
            data["multi_select"] = "1" if multi_select else "0"

    high_priority = awaiting_kind is not None
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
