"""SDK-message → backend-contract translation (the ``claude_sdk`` adapter).

``SdkTranslator`` is the single SDK-message-aware code path in OpenShrimp.
It is SDK-specific and lives inside the SDK adapter package — SDK message types
never escape this module.  The SDK ``BackendClient.receive_response`` applies
a per-session instance to each message, so everything downstream consumes
``open_shrimp.backend.types`` only.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

# SDK message types are imported (aliased) ONLY for the translation function
# below.  Everything downstream sees open_shrimp.backend.types.
from claude_agent_sdk import (
    AssistantMessage as _SdkAssistant,
    ResultMessage as _SdkResult,
    SystemMessage as _SdkSystem,
    TextBlock as _SdkText,
    ToolResultBlock as _SdkToolResult,
    ToolUseBlock as _SdkToolUse,
    UserMessage as _SdkUser,
)
from claude_agent_sdk.types import (
    RateLimitEvent as _SdkRateLimit,
    StreamEvent as _SdkStream,
    TaskNotificationMessage as _SdkTaskNotif,
    TaskProgressMessage as _SdkTaskProgress,
    TaskStartedMessage as _SdkTaskStarted,
    TaskUpdatedMessage as _SdkTaskUpdated,
)

from open_shrimp.backend import types as bt

logger = logging.getLogger(__name__)


def _block(b: Any) -> Any:
    """Translate one SDK content block into its backend.types equivalent.

    Used for ``AssistantMessage.content``, which only ever holds
    Text/ToolUse/ToolResult blocks.  Any other type passes through untouched
    (defensive — should not occur on this path).
    """
    if isinstance(b, _SdkText):
        return bt.TextBlock(text=b.text)
    if isinstance(b, _SdkToolUse):
        return bt.ToolUseBlock(id=b.id, name=b.name, input=b.input)
    if isinstance(b, _SdkToolResult):
        return bt.ToolResultBlock(
            tool_use_id=b.tool_use_id, content=b.content, is_error=b.is_error
        )
    return b


def _user_content(content: Any) -> Any:
    """Translate ``UserMessage.content``, preserving its dual shape.

    The SDK type is ``str | list[block...]`` where the list may contain block
    types the shared contract does not define (ThinkingBlock, ServerTool*).
    stream.py only acts on UserMessage when content is a ``list`` and inside it
    reads only ``ToolResultBlock``s.  So: pass a ``str`` through unchanged;
    for a ``list``, translate ToolResultBlocks (so the downstream
    ``isinstance(block, backend.ToolResultBlock)`` filter still selects them)
    and pass every other block through untouched.
    """
    if not isinstance(content, list):
        return content
    return [_block(b) if isinstance(b, _SdkToolResult) else b for b in content]


#: Cap on retained tool_use → (name, input) entries.  The SDK fires the
#: ``task_started`` event very shortly after the ``AssistantMessage`` that
#: triggered it, so we only need a handful of recent entries to correlate.
#: The cap keeps memory bounded for long-lived sessions; FIFO eviction.
_TOOL_USE_MAP_MAX = 256


class SdkTranslator:
    """Stateful per-session SDK→backend.types translator.

    Owns the bookkeeping needed to filter Claude Code CLI 2.1.117's
    auto-promotion of slow foreground Bash into task events
    (anthropics/claude-code#31518).  State is per-instance, and
    ``ClaudeSdkClient`` builds one per session, so concurrent chats
    do not share state.
    """

    def __init__(self) -> None:
        # tool_use_id → (tool_name, tool_input).  Populated as
        # AssistantMessage(ToolUseBlock) events flow through; consulted
        # when a TaskStartedMessage arrives to decide whether the task
        # is an auto-promoted FG Bash that should be filtered.  Bounded
        # via FIFO eviction (see _TOOL_USE_MAP_MAX) since the client is
        # long-lived per session and the matching task_started event
        # arrives within a few messages.
        self._tool_use_map: OrderedDict[str, tuple[str, dict[str, Any]]] = (
            OrderedDict()
        )
        # task_ids of auto-promoted FG Bash tasks; recorded when the
        # started event is dropped so the matching Progress/Notification
        # events can also be dropped.
        self._suppressed_task_ids: set[str] = set()

    def __call__(self, msg: Any) -> Any | None:
        """Convert one SDK message into the backend-neutral contract type.

        Order matters: Task* subclass SystemMessage in the SDK, so check
        them first.  Auto-promoted FG-bash task events are dropped
        (returns ``None``); the caller already skips ``None``.  Unknown
        types pass through raw (defensive).
        """
        if isinstance(msg, _SdkTaskStarted):
            if self._is_auto_promoted_fg_bash_started(msg):
                self._suppressed_task_ids.add(msg.task_id)
                logger.debug(
                    "Dropping auto-promoted FG bash task %s "
                    "(tool_use_id=%s)",
                    msg.task_id,
                    msg.tool_use_id,
                )
                return None
            return bt.TaskStartedMessage(
                subtype=msg.subtype,
                data=msg.data,
                task_id=msg.task_id,
                tool_use_id=msg.tool_use_id,
                description=msg.description,
                task_type=msg.task_type,
                output_file=getattr(msg, "output_file", None),
                session_id=msg.session_id,
            )
        if isinstance(msg, _SdkTaskProgress):
            if msg.task_id in self._suppressed_task_ids:
                return None
            return bt.TaskProgressMessage(
                subtype=msg.subtype,
                data=msg.data,
                task_id=msg.task_id,
                last_tool_name=getattr(msg, "last_tool_name", None),
                session_id=msg.session_id,
            )
        if isinstance(msg, _SdkTaskNotif):
            if msg.task_id in self._suppressed_task_ids:
                self._suppressed_task_ids.discard(msg.task_id)
                return None
            return bt.TaskNotificationMessage(
                subtype=msg.subtype,
                data=msg.data,
                task_id=msg.task_id,
                tool_use_id=msg.tool_use_id,
                output_file=msg.output_file,
                status=msg.status,
                summary=msg.summary,
                session_id=msg.session_id,
            )
        if isinstance(msg, _SdkTaskUpdated):
            # Subclasses SystemMessage, so it must be caught before the
            # generic _SdkSystem branch below.
            if msg.task_id in self._suppressed_task_ids:
                if msg.status in bt.TERMINAL_TASK_STATUSES:
                    self._suppressed_task_ids.discard(msg.task_id)
                return None
            return bt.TaskUpdatedMessage(
                subtype=msg.subtype,
                data=msg.data,
                task_id=msg.task_id,
                patch=msg.patch,
                status=msg.status,
                session_id=msg.session_id,
            )
        if isinstance(msg, _SdkSystem):
            return bt.SystemMessage(subtype=msg.subtype, data=msg.data)
        if isinstance(msg, _SdkAssistant):
            # Record tool_use → (name, input) so the FG-bash detector
            # can consult it when the task_started event arrives.
            for b in msg.content:
                if isinstance(b, _SdkToolUse):
                    self._tool_use_map[b.id] = (b.name, b.input)
                    while len(self._tool_use_map) > _TOOL_USE_MAP_MAX:
                        self._tool_use_map.popitem(last=False)
            return bt.AssistantMessage(
                content=[_block(b) for b in msg.content],
                usage=msg.usage,
                error=msg.error,
                session_id=msg.session_id,
                parent_tool_use_id=getattr(msg, "parent_tool_use_id", None),
            )
        if isinstance(msg, _SdkUser):
            return bt.UserMessage(
                content=_user_content(msg.content),
                parent_tool_use_id=getattr(msg, "parent_tool_use_id", None),
            )
        if isinstance(msg, _SdkResult):
            return bt.ResultMessage(
                session_id=msg.session_id,
                total_cost_usd=msg.total_cost_usd,
                usage=msg.usage,
                model_usage=msg.model_usage,
                num_turns=msg.num_turns,
                duration_ms=msg.duration_ms,
                errors=msg.errors,
                is_error=msg.is_error,
            )
        if isinstance(msg, _SdkStream):
            raw = msg.event
            if isinstance(raw, dict) and raw.get("type") == "content_block_delta":
                delta = raw.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    candidate = delta.get("text", "")
                    if isinstance(candidate, str) and candidate:
                        return bt.TextDeltaEvent(
                            text=candidate,
                            session_id=msg.session_id,
                            parent_tool_use_id=getattr(msg, "parent_tool_use_id", None),
                        )
            return None
        if isinstance(msg, _SdkRateLimit):
            info = msg.rate_limit_info
            return bt.RateLimitEvent(
                status=info.status,
                rate_limit_type=info.rate_limit_type,
                resets_at=info.resets_at,
                utilization=info.utilization,
                session_id=msg.session_id,
            )
        logger.debug("Unknown SDK message type passed through: %s", type(msg).__name__)
        return msg

    def _is_auto_promoted_fg_bash_started(
        self, msg: _SdkTaskStarted
    ) -> bool:
        """Detect SDK auto-promotion of slow foreground Bash to a task.

        Why: the Claude Code CLI silently wraps long-running foreground Bash
        (minutes-scale) in ``task_started`` / ``task_notification`` events
        even when ``run_in_background`` was not set.  Those auto-promoted
        tasks have an empty ``output_file`` and no ``.output`` file on disk —
        the regular Bash tool-result flow already rendered the output.
        Without this filter we'd post ⏳ + 📋 noise and a "View output"
        button that 404s.  Reported upstream as anthropics/claude-code#31518
        (closed without fix).
        """
        if msg.task_type != "local_bash" or not msg.tool_use_id:
            return False
        info = self._tool_use_map.get(msg.tool_use_id)
        if info is None or info[0] != "Bash":
            return False
        return not info[1].get("run_in_background")


__all__ = ["SdkTranslator"]
