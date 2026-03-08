"""PreToolUse hooks for tool approval.

Intercepts tool calls from the Claude Agent SDK. Auto-approved tools pass
through immediately; all others trigger a callback (e.g. Telegram inline
keyboard) and await the user's decision.

AskUserQuestion is handled specially: the hook presents questions to the user
via Telegram, collects answers, then denies the tool (to prevent the CLI from
trying its own interactive UI) while passing the answers back via
additionalContext so Claude receives them.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import HookContext, PreToolUseHookInput
from claude_agent_sdk.types import SyncHookJSONOutput

logger = logging.getLogger(__name__)

# Type for the approval callback: receives tool_name, tool_input dict,
# and tool_use_id; returns True (allow) or False (deny).
ApprovalCallback = Callable[[str, dict[str, Any], str], Awaitable[bool]]

# Type for the question callback: receives list of question dicts,
# returns answers dict mapping question text -> answer string.
QuestionCallback = Callable[[list[dict[str, Any]]], Awaitable[dict[str, str]]]


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
    handle_user_questions: QuestionCallback | None = None,
) -> Callable[[PreToolUseHookInput, str | None, HookContext], Awaitable[SyncHookJSONOutput]]:
    """Create a PreToolUse hook function bound to a context's settings.

    Args:
        auto_approve_tools: Tool names that are auto-approved (e.g. Read, Glob).
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
        handle_user_questions: Optional async callback for AskUserQuestion.
            Receives the questions list, returns answers dict.
    """

    async def tool_approval_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput:
        tool_name: str = input_data.get("tool_name", "")
        tool_input: dict[str, Any] = input_data.get("tool_input", {})

        # Special handling for AskUserQuestion: present questions to user
        # via Telegram, collect answers, then DENY the tool to prevent the
        # CLI from trying its own interactive UI.  The user's answers are
        # passed back to Claude via additionalContext so it can use them.
        if tool_name == "AskUserQuestion" and handle_user_questions:
            questions = tool_input.get("questions", [])
            logger.info("AskUserQuestion with %d question(s)", len(questions))
            answers = await handle_user_questions(questions)
            logger.info("Collected answers for AskUserQuestion: %s", answers)

            # Format answers for Claude to consume
            answer_lines = []
            for question_text, answer in answers.items():
                answer_lines.append(f"Q: {question_text}\nA: {answer}")
            answers_text = "\n\n".join(answer_lines)

            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "The user has already answered these questions via the "
                        "Telegram interface. Do not retry this tool call. "
                        "Here are their responses:\n\n" + answers_text
                    ),
                }
            }

        if tool_name in auto_approve_tools:
            logger.debug("Auto-approved tool: %s", tool_name)
            return _make_hook_response("allow")

        logger.info("Requesting approval for tool: %s (id=%s)", tool_name, tool_use_id)
        approved = await request_approval(tool_name, tool_input, tool_use_id or "")
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s (id=%s)", tool_name, decision, tool_use_id)
        return _make_hook_response(decision)

    return tool_approval_hook
