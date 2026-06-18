"""Backend-neutral message, content-block, and permission contracts.

This is the shared type contract imported by every backend's stream layer.
It is lifted from ``origin/opencode``'s ``opencode_client/events.py`` (which
already proved out exactly these dataclasses) and extended with the
``Task*`` / ``RateLimitEvent`` shapes and the ``session_id`` fields the
SDK path carries.  See ``docs/step1-type-contract-implementation-plan.md``.

No backend-specific types appear here.  The SDK path translates each SDK
message into one of these instances in ``agent.py:_to_backend_event``; the
``opencode`` / ``pty_jsonl`` backends construct them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# --- Section 2: permissions -------------------------------------------------
# Lifted verbatim from opencode's events.py; pure data, uniform across both
# branches.  As of step 2b, hooks.make_can_use_tool returns *these* neutral
# results unconditionally; the claude_sdk backend translates them to the SDK's
# permission types at its options seam (claude_sdk/permission.py) to satisfy
# the SDK's isinstance contract.  No src/ module outside that adapter imports
# the SDK permission types.


@dataclass
class ToolPermissionContext:
    signal: Any | None = None
    suggestions: list[Any] = field(default_factory=list)
    always_patterns: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    agent_id: str | None = None


@dataclass
class PermissionResultAllow:
    # updated_input / updated_permissions are accepted for SDK-callback
    # signature parity but ignored by non-SDK bridges.
    behavior: Literal["allow"] = "allow"
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[Any] | None = None
    reply: Literal["once", "always"] = "once"


@dataclass
class PermissionResultDeny:
    behavior: Literal["deny"] = "deny"
    message: str = ""
    interrupt: bool = False


PermissionResult = Union[PermissionResultAllow, PermissionResultDeny]


# --- Section 1: content blocks ----------------------------------------------


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str = ""
    name: str = ""  # canonical mcp__openshrimp__* prefix preserved
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    tool_use_id: str = ""
    content: Any = None
    is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


# --- Section 1: messages ----------------------------------------------------


@dataclass
class AssistantMessage:
    """An assistant turn — text and/or tool-use blocks.

    ``session_id`` is carried for early-capture so a cancel before the
    ``ResultMessage`` still records the session.
    """

    content: list[ContentBlock]
    usage: dict[str, Any] | None = None
    error: str | None = None
    session_id: str | None = None
    parent_tool_use_id: str | None = None


@dataclass
class UserMessage:
    # content may also be a plain ``str``; the SDK union additionally allows
    # block types this contract does not define (ThinkingBlock, ServerTool*).
    # _to_backend_event's _user_content preserves both shapes.
    content: Any
    parent_tool_use_id: str | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]
    # NO session_id — matches the SDK; stream.py's getattr() guard tolerates
    # its absence (session_id arrives via Task* / ResultMessage instead).


@dataclass
class ResultMessage:
    session_id: str
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    model_usage: dict[str, dict[str, Any]] | None = None
    num_turns: int | None = None  # opencode maps its num_steps here (decision 2)
    duration_ms: int | None = None
    errors: list[dict[str, Any]] | None = None
    is_error: bool = False


@dataclass
class StreamEvent:
    event: dict[str, Any]
    session_id: str | None = None
    parent_tool_use_id: str | None = None


# Task* mirror the SDK inheritance (decision 4) so stream.py's nested
# isinstance dispatch — Task* checks live *inside* the isinstance(SystemMessage)
# branch — is untouched.  Added fields carry defaults so the dataclass-generated
# __init__ stays valid despite SystemMessage's required fields.


@dataclass
class TaskStartedMessage(SystemMessage):
    task_id: str = ""
    tool_use_id: str | None = None
    description: str | None = None
    task_type: str | None = None
    output_file: str | None = None
    session_id: str | None = None


@dataclass
class TaskProgressMessage(SystemMessage):
    task_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    last_tool_name: str | None = None
    session_id: str | None = None


@dataclass
class TaskNotificationMessage(SystemMessage):
    task_id: str = ""
    tool_use_id: str | None = None
    output_file: str | None = None
    status: str | None = None
    summary: str | None = None
    session_id: str | None = None


@dataclass
class RateLimitEvent:
    # Flat, vendor-neutral shape: the SDK nests these under
    # rate_limit_info: RateLimitInfo; _to_backend_event flattens it here.
    status: str = "allowed"  # "allowed" | "allowed_warning" | "rejected"
    rate_limit_type: str | None = None
    resets_at: int | None = None
    utilization: float | None = None
    session_id: str | None = None


Message = Union[
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    StreamEvent,
    TaskStartedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
    RateLimitEvent,
]


__all__ = [
    "AssistantMessage",
    "ContentBlock",
    "Message",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "RateLimitEvent",
    "ResultMessage",
    "StreamEvent",
    "SystemMessage",
    "TaskNotificationMessage",
    "TaskProgressMessage",
    "TaskStartedMessage",
    "TextBlock",
    "ToolPermissionContext",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
