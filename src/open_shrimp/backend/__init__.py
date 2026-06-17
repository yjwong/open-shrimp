"""Backend-neutral contract surface for OpenShrimp.

A single shared contract imported by every backend's stream layer: the
pure-data types (messages, content blocks, permissions, :class:`SessionInfo`,
the error aliases), the live ``Backend`` / ``BackendClient`` protocols and
``BackendOptions``, and the config-driven factory (:func:`get_backend`).

A concrete backend (e.g. ``claude_sdk``) lives in its own subpackage and is
selected once at startup from the top-level ``backend`` config key.
"""

from __future__ import annotations

from open_shrimp.backend.errors import CLIConnectionError, ProcessError
from open_shrimp.backend.factory import (
    DEFAULT_BACKEND,
    get_backend,
    known_backends,
)
from open_shrimp.backend.protocol import (
    Backend,
    BackendClient,
    BackendOptions,
    CanUseTool,
    ToolFactory,
)
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.tools import serve_tools_over_mcp_http
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
    "Backend",
    "BackendClient",
    "BackendOptions",
    "CanUseTool",
    "CLIConnectionError",
    "ContentBlock",
    "DEFAULT_BACKEND",
    "get_backend",
    "known_backends",
    "Message",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ProcessError",
    "RateLimitEvent",
    "ResultMessage",
    "SessionInfo",
    "serve_tools_over_mcp_http",
    "StreamEvent",
    "SystemMessage",
    "TaskNotificationMessage",
    "TaskProgressMessage",
    "TaskStartedMessage",
    "TextBlock",
    "ToolFactory",
    "ToolPermissionContext",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
