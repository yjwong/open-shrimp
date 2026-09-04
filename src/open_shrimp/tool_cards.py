"""Render a tool call as a collapsible card.

Pure string building over a tool's input and result — no ``Bot``, no draft
state, no event loop.  It lives apart from ``stream.py`` so asserting what a
finished command's summary row says does not mean driving a fake event stream
through the send path.

A card is one row when collapsed, which is the point: a turn with a dozen tool
calls costs a dozen rows, and the output is a tap away rather than a scroll.
"""

from __future__ import annotations

from typing import Any

from open_shrimp.markdown import (
    escape_rich_inline,
    rich_code_block,
    rich_details,
)

# Maximum lines of tool output to fold into a card.
TOOL_OUTPUT_MAX_LINES = 50
# Maximum characters of tool output to fold into a card.
TOOL_OUTPUT_MAX_CHARS = 1500
# Longest command that fits on a collapsed card's summary row.
SUMMARY_COMMAND_MAX_CHARS = 80
# Terminal states of a Bash card.  A running card carries neither: between the
# agent issuing the command and the result arriving it may be queued, waiting
# on an approval tap that never comes, or executing, and the card claims none
# of them.
BASH_NO_OUTPUT_NOTE = "*No output.*"
BASH_INTERRUPTED_NOTE = "*Interrupted.*"


def truncate_output(text: str) -> tuple[str, bool]:
    """Cap output at the card limits, keeping the tail — the recent output."""
    lines = text.splitlines()
    truncated = False
    if len(lines) > TOOL_OUTPUT_MAX_LINES:
        lines = lines[-TOOL_OUTPUT_MAX_LINES:]
        truncated = True

    result = "\n".join(lines)
    if len(result) > TOOL_OUTPUT_MAX_CHARS:
        result = result[-TOOL_OUTPUT_MAX_CHARS:]
        truncated = True

    if truncated:
        result = "…(truncated)\n" + result
    return result, truncated


def tool_summary_row(
    tool_name: str,
    summary: str,
    auto: bool,
    *,
    is_error: bool = False,
) -> str:
    """Build the one row a tool call costs when its card is collapsed."""
    icon = "⚠️" if is_error else "🔧"
    row = f"{icon} **{escape_rich_inline(tool_name)}**"
    if summary:
        row += f" — {escape_rich_inline(summary)}"
    if auto:
        row += " *(auto)*"
    return row


def bash_summary(
    tool_input: dict[str, Any],
    icon: str,
    label: str,
    *,
    elapsed: float | None = None,
    is_error: bool = False,
) -> str:
    """Build the one row a collapsed Bash card shows.

    The agent's own description of the command is what the row says; the
    command itself is in the card body a tap away, and wrapping a pipeline
    across three lines buries the rest of the row.  A call that came without
    a description falls back to the command, clipped.

    The elapsed time is measured from the agent issuing the command to the
    result landing, so it counts an approval wait as well as the run.
    """
    parts: list[str] = []
    description = (tool_input.get("description") or "").strip()
    if description:
        parts.append(escape_rich_inline(description))
    else:
        command = " ".join((tool_input.get("command") or "").split())
        if len(command) > SUMMARY_COMMAND_MAX_CHARS:
            command = command[:SUMMARY_COMMAND_MAX_CHARS - 1] + "…"
        if command:
            parts.append(f"`{command}`")
    if is_error:
        parts.append("**failed**")
    if elapsed is not None:
        if elapsed < 60:
            parts.append(f"{elapsed:.1f}s")
        else:
            minutes, rest = divmod(int(elapsed), 60)
            parts.append(f"{minutes}m{rest:02d}s")

    row = f"{icon} **{escape_rich_inline(label)}**"
    return f"{row} — {' · '.join(parts)}" if parts else row


def bash_card(
    tool_input: dict[str, Any],
    icon: str,
    label: str,
    *,
    output: str | None = None,
    note: str | None = None,
    elapsed: float | None = None,
    is_error: bool = False,
    open: bool = True,
) -> str:
    """Render a Bash invocation as a collapsible card.

    Open while the command runs, so the command is readable without a tap;
    collapsed once the result is in, so a finished command costs one row.
    """
    summary = bash_summary(
        tool_input, icon, label, elapsed=elapsed, is_error=is_error,
    )
    body_parts = [rich_code_block(tool_input.get("command") or "", "bash")]
    if output:
        body_parts.append(rich_code_block(output))
    if note:
        body_parts.append(note)
    return rich_details(summary, "\n\n".join(body_parts), open=open)
