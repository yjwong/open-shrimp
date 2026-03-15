"""Tool approval via Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_udang.handlers.state import (
    _approval_futures,
    _approval_tool_names,
    _pending_agent_inputs,
)
from open_udang.handlers.utils import _escape_mdv2
from open_udang.stream import _relative_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_edit_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format an Edit tool call as a unified diff for the approval prompt."""
    file_path = _relative_path(tool_input.get("file_path", "unknown"), cwd)
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\u270f\ufe0f *Edit:* `{escaped_path}`"

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, lineterm="",
    ))

    if diff_lines:
        # Drop the ---/+++ header lines from unified_diff, keep @@ and content
        diff_body = "\n".join(diff_lines[2:])
    else:
        diff_body = "(no diff)"

    # Truncate if the diff is too long for a single Telegram message.
    # Reserve space for the header, code fences, and buttons (~200 chars).
    max_diff_len = 4096 - 200
    if len(diff_body) > max_diff_len:
        diff_body = diff_body[:max_diff_len] + "\n..."

    escaped_diff = _escape_mdv2(diff_body)
    return f"{header}\n\n```diff\n{escaped_diff}\n```"


def _format_bash_approval(tool_input: dict[str, Any]) -> str:
    """Format a Bash tool call for the approval prompt."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    parts: list[str] = []
    if description:
        parts.append(f"\U0001f4bb *Bash:* {_escape_mdv2(description)}")
    else:
        parts.append("\U0001f4bb *Bash*")

    # Show the command in a code block.
    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    escaped_cmd = _escape_mdv2(command)
    parts.append(f"```bash\n{escaped_cmd}\n```")

    return "\n\n".join(parts)


def _format_write_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format a Write tool call for the approval prompt."""
    file_path = _relative_path(tool_input.get("file_path", "unknown"), cwd)
    content = tool_input.get("content", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\U0001f4dd *Write:* `{escaped_path}`"

    # Truncate if the content is too long for a single Telegram message.
    max_content_len = 4096 - 200
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n..."

    escaped_content = _escape_mdv2(content)
    return f"{header}\n\n```\n{escaped_content}\n```"


def _format_agent_approval(tool_input: dict[str, Any], expanded: bool = False) -> str:
    """Format an Agent tool call for the approval prompt.

    Shows a compact view with description and subagent type by default.
    When expanded=True, appends the full prompt text.
    """
    description = tool_input.get("description", "")
    subagent_type = tool_input.get("subagent_type", "")
    prompt = tool_input.get("prompt", "")

    parts: list[str] = []

    # Header with subagent type
    if subagent_type:
        parts.append(f"\U0001f916 *Agent* \\({_escape_mdv2(subagent_type)}\\)")
    else:
        parts.append("\U0001f916 *Agent*")

    # Description line
    if description:
        parts.append(_escape_mdv2(description))

    # Full prompt (only when expanded)
    if expanded and prompt:
        max_prompt_len = 4096 - 300
        display_prompt = prompt
        if len(display_prompt) > max_prompt_len:
            display_prompt = display_prompt[:max_prompt_len] + "\n..."
        parts.append(f"```\n{_escape_mdv2(display_prompt)}\n```")

    return "\n\n".join(parts)


def _format_generic_approval(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a generic tool call for the approval prompt."""
    summary_parts = [f"*Tool:* `{tool_name}`"]
    for key, val in tool_input.items():
        val_str = str(val)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        key_escaped = key.replace("_", "\\_")
        val_escaped = _escape_mdv2(val_str)
        summary_parts.append(f"*{key_escaped}:* {val_escaped}")
    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
# Approval keyboard & auto-approved diff notification
# ---------------------------------------------------------------------------


async def _send_auto_approved_diff(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str | None = None,
) -> None:
    """Send a read-only diff message for an auto-approved edit.

    Similar to the approval keyboard but without buttons -- just shows the
    diff so the user can see what changed even when "accept all edits" is
    active.
    """
    if tool_name == "Edit":
        text = _format_edit_approval(tool_input, cwd=cwd)
    elif tool_name == "Write":
        text = _format_write_approval(tool_input, cwd=cwd)
    else:
        text = _format_generic_approval(tool_name, tool_input)

    text += f"\n\u2705 _Auto\\-approved_"

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
        )
    except Exception:
        logger.exception("Failed to send auto-approved diff notification")


async def _send_approval_keyboard(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    cwd: str | None = None,
) -> bool:
    """Send an inline keyboard for tool approval and wait for response."""
    if tool_name == "Edit":
        text = _format_edit_approval(tool_input, cwd=cwd)
    elif tool_name == "Bash":
        text = _format_bash_approval(tool_input)
    elif tool_name == "Write":
        text = _format_write_approval(tool_input, cwd=cwd)
    elif tool_name == "Agent":
        text = _format_agent_approval(tool_input, expanded=False)
    else:
        text = _format_generic_approval(tool_name, tool_input)

    approve_data = f"approve:{tool_use_id}"
    deny_data = f"deny:{tool_use_id}"
    _approval_tool_names[tool_use_id] = tool_name

    # Build keyboard buttons -- some tools get extra buttons
    buttons = []
    if tool_name == "Agent":
        show_prompt_data = f"show_prompt:{tool_use_id}"
        _pending_agent_inputs[tool_use_id] = tool_input
        buttons.append(InlineKeyboardButton("Show prompt", callback_data=show_prompt_data))
    buttons.append(InlineKeyboardButton("Approve", callback_data=approve_data))
    # Edit and Write get an "Accept all edits" option for session-scoped
    # auto-approval of mutating file operations within the working directory.
    if tool_name in ("Edit", "Write"):
        accept_all_data = f"accept_all_edits:{tool_use_id}"
        buttons.append(InlineKeyboardButton("Accept all edits", callback_data=accept_all_data))
    buttons.append(InlineKeyboardButton("Deny", callback_data=deny_data))

    keyboard = InlineKeyboardMarkup([buttons])

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )

    # Create a future and wait for the callback
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    if tool_name in ("Edit", "Write"):
        _approval_futures[f"accept_all_edits:{tool_use_id}"] = future

    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_futures.pop(f"accept_all_edits:{tool_use_id}", None)
        _pending_agent_inputs.pop(tool_use_id, None)
        _approval_tool_names.pop(tool_use_id, None)


# ---------------------------------------------------------------------------
# Callback query handling for approval-related buttons
# ---------------------------------------------------------------------------


async def handle_approval_callback(
    query: Any,
    data: str,
    config: Any,
    context: Any,
) -> bool:
    """Handle approval-related callback queries.

    Handles: approve:*, deny:*, show_prompt:*, show_bash:*, accept_all_edits:*.
    Returns True if the callback was handled.
    """
    import aiosqlite

    from open_udang.handlers.state import _edit_approved_sessions
    from open_udang.handlers.utils import _get_context
    from open_udang.stream import _bash_output_store

    # Handle "Show prompt" expansion for Agent tool
    if data.startswith("show_prompt:"):
        tool_use_id = data[len("show_prompt:"):]
        tool_input = _pending_agent_inputs.get(tool_use_id)
        if not tool_input:
            await query.answer("Prompt data no longer available.")
            return True

        await query.answer()

        # Re-render the message with expanded prompt, remove "Show prompt" button
        if query.message:
            expanded_text = _format_agent_approval(tool_input, expanded=True)
            approve_data = f"approve:{tool_use_id}"
            deny_data = f"deny:{tool_use_id}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Approve", callback_data=approve_data),
                        InlineKeyboardButton("Deny", callback_data=deny_data),
                    ]
                ]
            )
            try:
                await query.message.edit_text(
                    text=expanded_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to expand Agent prompt")
        return True

    # Handle "Show output" for Bash tool results
    if data.startswith("show_bash:"):
        formatted_output = _bash_output_store.pop(data, None)
        if not formatted_output:
            await query.answer("Output data no longer available.")
            return True

        await query.answer()

        if query.message:
            from open_udang.markdown import gfm_to_telegram

            chunks = gfm_to_telegram(formatted_output)
            expanded_text = chunks[0] if chunks else ""
            try:
                await query.message.edit_text(
                    text=expanded_text,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to expand Bash output")
                # Fallback: just remove the button
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to remove bash button")
        return True

    # Handle "Accept all edits" -- approve this tool and enable auto-approval
    # for all future Edit/Write calls within cwd for this session.
    if data.startswith("accept_all_edits:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Determine the chat's active context to scope the flag
        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None:
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(chat_id, config, db)
            _edit_approved_sessions.add((chat_id, ctx_name))
            logger.info(
                "Accept-all-edits enabled for chat %d context %s",
                chat_id,
                ctx_name,
            )

        future.set_result(True)
        await query.answer("Approved. All future edits will be auto-approved.")

        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                status = "\n\n\u2705 *Approved\\.* _All future edits auto\\-approved\\._"
                await query.message.edit_text(
                    text=original_md + status,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")
        return True

    # Handle approve/deny
    if data.startswith("approve:") or data.startswith("deny:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        approved = data.startswith("approve:")
        future.set_result(approved)

        # Extract tool_use_id from callback data (format: "approve:<id>" or "deny:<id>")
        tool_use_id = data.split(":", 1)[1] if ":" in data else ""
        tool_name = _approval_tool_names.get(tool_use_id, "")

        action = "Approved" if approved else "Denied"
        await query.answer(f"{action}.")

        # Update the message to show the decision (remove buttons, append status).
        # For Bash, collapse to a compact one-liner since the "Show output" button
        # message that follows will show the command again -- avoids duplication.
        if query.message:
            try:
                if tool_name == "Bash":
                    icon = '\u2705' if approved else '\u274c'
                    compact = f"{icon} *{_escape_mdv2(tool_name)}* \u2014 {action}\\."
                    await query.message.edit_text(
                        text=compact,
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                else:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    icon = '\u2705' if approved else '\u274c'
                    status = f"\n\n{icon} *{action}\\.*"
                    await query.message.edit_text(
                        text=original_md + status,
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
            except Exception:
                # Fallback: just remove the keyboard without modifying text
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")
        return True

    return False
