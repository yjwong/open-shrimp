"""Backend-neutral contract surface for OpenShrimp.

A single shared type contract imported by every backend's stream layer.
Step 1 lands the pure-data contracts (messages, content blocks, permissions,
:class:`SessionInfo`, the error aliases); the live ``Backend`` /
``BackendClient`` protocols and the config-driven factory follow in step 3.

See ``docs/step1-type-contract-implementation-plan.md``.
"""

from __future__ import annotations

from open_shrimp.backend.errors import CLIConnectionError, ProcessError
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.types import (
    AssistantMessage,
    ContentBlock,
    Message,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

__all__ = [
    "AssistantMessage",
    "CLIConnectionError",
    "ContentBlock",
    "Message",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ProcessError",
    "RateLimitEvent",
    "ResultMessage",
    "SessionInfo",
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
