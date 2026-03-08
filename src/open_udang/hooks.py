"""PreToolUse hooks for tool approval.

Intercepts tool calls from the Claude Agent SDK. Auto-approved tools pass
through immediately; all others trigger a callback (e.g. Telegram inline
keyboard) and await the user's decision.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import HookContext, PreToolUseHookInput
from claude_agent_sdk.types import SyncHookJSONOutput

logger = logging.getLogger(__name__)

# Type for the approval callback: receives tool_name, tool_input dict,
# and tool_use_id; returns True (allow) or False (deny).
ApprovalCallback = Callable[[str, dict[str, Any], str], Awaitable[bool]]


def _make_hook_response(decision: str) -> SyncHookJSONOutput:
    """Build the PreToolUse hook response dict."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }


def make_tool_approval_hook(
    auto_approve_tools: list[str],
    request_approval: ApprovalCallback,
) -> Callable[[PreToolUseHookInput, str | None, HookContext], Awaitable[SyncHookJSONOutput]]:
    """Create a PreToolUse hook function bound to a context's settings.

    Args:
        auto_approve_tools: Tool names that are auto-approved (e.g. Read, Glob).
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
    """

    async def tool_approval_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput:
        tool_name: str = input_data.get("tool_name", "")
        tool_input: dict[str, Any] = input_data.get("tool_input", {})

        if tool_name in auto_approve_tools:
            logger.debug("Auto-approved tool: %s", tool_name)
            return _make_hook_response("allow")

        logger.info("Requesting approval for tool: %s (id=%s)", tool_name, tool_use_id)
        approved = await request_approval(tool_name, tool_input, tool_use_id or "")
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s (id=%s)", tool_name, decision, tool_use_id)
        return _make_hook_response(decision)

    return tool_approval_hook
