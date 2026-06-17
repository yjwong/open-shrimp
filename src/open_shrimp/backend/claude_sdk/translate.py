"""SDK-message → backend-contract translation (the ``claude_sdk`` adapter).

``_to_backend_event`` is the single SDK-message-aware code path in OpenShrimp.
It was moved here **verbatim** from ``agent.py`` in step 3 because it is
SDK-specific and therefore belongs inside the SDK adapter package — SDK message
types never escape this module.  The SDK ``BackendClient.receive_response``
applies it to each message, so everything downstream consumes
``open_shrimp.backend.types`` only.
"""

from __future__ import annotations

import logging
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


def _to_backend_event(msg: Any) -> Any:
    """Convert one SDK message into the backend-neutral contract type.

    The ONLY SDK-message-aware code path on master after step 1.  Order
    matters: Task* subclass SystemMessage in the SDK, so check them first.
    Unknown types pass through raw (defensive).
    """
    if isinstance(msg, _SdkTaskStarted):
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
        return bt.TaskProgressMessage(
            subtype=msg.subtype,
            data=msg.data,
            task_id=msg.task_id,
            last_tool_name=getattr(msg, "last_tool_name", None),
            session_id=msg.session_id,
        )
    if isinstance(msg, _SdkTaskNotif):
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
    if isinstance(msg, _SdkSystem):
        return bt.SystemMessage(subtype=msg.subtype, data=msg.data)
    if isinstance(msg, _SdkAssistant):
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
        return bt.StreamEvent(
            event=msg.event,
            session_id=msg.session_id,
            parent_tool_use_id=getattr(msg, "parent_tool_use_id", None),
        )
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


__all__ = ["_to_backend_event"]
