"""Approval-card bodies that read the same whichever backend asked.

A Bash call, an Agent call and a tool nobody wrote a renderer for look
identical under both backends — the tool names differ, the card does not.  They
live here rather than twice so a change to how a command is shown is one edit,
not two that can drift.

Cards whose *content* is backend-specific — an edit diff, an ``apply_patch``
envelope, a Monitor's persistence flag — stay in the backend that understands
them.
"""

from __future__ import annotations

from typing import Any

from open_shrimp.markdown import (
    RICH_MAX_BODY,
    escape_rich,
    escape_rich_inline,
    rich_code_block,
)

# A generic card shows every argument, so each one is clipped hard: the point
# is to say what the tool was asked to do, not to reproduce its input.
GENERIC_VALUE_MAX_CHARS = 200


def _clip(text: str) -> str:
    if len(text) > RICH_MAX_BODY:
        return text[:RICH_MAX_BODY] + "\n..."
    return text


def format_bash_approval(tool_input: dict[str, Any]) -> str:
    """Format a Bash tool call for the approval prompt."""
    description = tool_input.get("description", "")
    header = (
        f"\U0001f4bb **Bash:** {escape_rich_inline(description)}"
        if description
        else "\U0001f4bb **Bash**"
    )
    command = _clip(tool_input.get("command", ""))
    return f"{header}\n\n{rich_code_block(command, 'bash')}"


def format_write_approval(tool_input: dict[str, Any], file_path: str) -> str:
    """Format a Write tool call for the approval prompt.

    The path arrives resolved: the SDK calls it ``file_path`` and OpenCode
    ``filePath``, and which one to read is the caller's business.
    """
    content = _clip(tool_input.get("content", ""))
    return f"\U0001f4dd **Write:** `{file_path}`\n\n{rich_code_block(content)}"


def format_agent_approval(
    tool_input: dict[str, Any], expanded: bool = False,
) -> str:
    """Format an Agent tool call for the approval prompt.

    The prompt itself is shown only once the user taps "Show prompt": it is
    the longest thing on any card and the decision rarely turns on it.
    """
    subagent_type = tool_input.get("subagent_type", "")
    description = tool_input.get("description", "")
    prompt = tool_input.get("prompt", "")

    parts = [
        f"\U0001f916 **Agent** ({escape_rich_inline(subagent_type)})"
        if subagent_type
        else "\U0001f916 **Agent**"
    ]
    if description:
        parts.append(escape_rich(description))
    if expanded and prompt:
        parts.append(rich_code_block(_clip(prompt)))
    return "\n\n".join(parts)


def format_generic_approval(
    tool_name: str, tool_input: dict[str, Any],
) -> str:
    """Format a tool with no renderer of its own: its name and its arguments."""
    rows = [f"**Tool:** `{tool_name}`"]
    for key, value in tool_input.items():
        text = str(value)
        if len(text) > GENERIC_VALUE_MAX_CHARS:
            text = text[:GENERIC_VALUE_MAX_CHARS] + "..."
        rows.append(f"**{escape_rich_inline(key)}:** {escape_rich(text)}")
    return "<br>".join(rows)
