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
from open_udang.hooks import ApprovalRule
from open_udang.stream import _relative_path

logger = logging.getLogger(__name__)


# Prefixes to skip when extracting the bash command name (e.g. "sudo git").
_BASH_SKIP_PREFIXES = {"sudo", "env", "nohup", "nice", "ionice", "time", "strace"}


def _extract_bash_prefix(command: str) -> str | None:
    """Extract the primary command name from a bash command string.

    Handles chained commands (``&&``, ``||``, ``;``), skips common prefixes
    like ``sudo`` and ``env VAR=val``, and returns the first significant
    word.  Returns None if the command is too complex to extract a useful
    prefix (e.g. starts with a subshell or heredoc).
    """
    cmd = command.strip()
    if not cmd or cmd.startswith("(") or cmd.startswith("{"):
        return None

    # Take only the first command in a chain.
    for sep in ("&&", "||", ";"):
        cmd = cmd.split(sep, 1)[0].strip()

    # Handle pipes: take only the first segment.
    cmd = cmd.split("|", 1)[0].strip()

    words = cmd.split()
    if not words:
        return None

    # Skip common prefixes and their flags/arguments.
    idx = 0
    in_prefix = True
    while idx < len(words) and in_prefix:
        word = words[idx]
        if word in _BASH_SKIP_PREFIXES:
            idx += 1
            # Skip any flags that belong to the prefix command
            # (e.g. "nice -n 10", "sudo -u user").
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
                # Skip the flag's argument if it looks like a value
                if idx < len(words) and not words[idx].startswith("-"):
                    idx += 1
            continue
        # env VAR=val ... — skip variable assignments.
        if "=" in word and idx > 0:
            idx += 1
            continue
        in_prefix = False

    if idx >= len(words):
        return None

    prefix = words[idx]
    # Reject if it looks like a path to a script rather than a command name.
    if "/" in prefix and not prefix.startswith("./"):
        return None

    return prefix


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
    thread_id: int | None = None,
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

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_notification=True,
            **thread_kwargs,
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
    thread_id: int | None = None,
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

    # Build keyboard rows -- primary actions on top, session-scoped on bottom.
    # Row 1: [Approve] [Deny] (and optional [Show prompt] for Agent)
    primary_row: list[InlineKeyboardButton] = []
    if tool_name == "Agent":
        show_prompt_data = f"show_prompt:{tool_use_id}"
        _pending_agent_inputs[tool_use_id] = tool_input
        primary_row.append(InlineKeyboardButton("Show prompt", callback_data=show_prompt_data))
    primary_row.append(InlineKeyboardButton("Approve", callback_data=approve_data))
    primary_row.append(InlineKeyboardButton("Deny", callback_data=deny_data))

    # Row 2: session-scoped auto-approval buttons
    session_row: list[InlineKeyboardButton] = []
    # Edit and Write get an "Accept all edits" option for session-scoped
    # auto-approval of mutating file operations within the working directory.
    if tool_name in ("Edit", "Write"):
        accept_all_data = f"accept_all_edits:{tool_use_id}"
        session_row.append(InlineKeyboardButton("Accept all edits", callback_data=accept_all_data))
    # For Bash, offer a prefix-specific button (e.g. "Accept all git")
    # before the blanket "Accept all Bash" button.
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        prefix = _extract_bash_prefix(command)
        if prefix:
            accept_prefix_data = f"accept_bash_pfx:{tool_use_id}:{prefix}"
            if len(accept_prefix_data.encode()) <= 64:
                session_row.append(InlineKeyboardButton(
                    f"Accept all {prefix}", callback_data=accept_prefix_data,
                ))
    # All tools (except Edit/Write which have the more specific "Accept all
    # edits" button) get a generic "Accept all <tool>" option for session-
    # scoped auto-approval of that specific tool type.
    if tool_name not in ("Edit", "Write"):
        accept_all_tool_data = f"accept_all_tool:{tool_use_id}:{tool_name}"
        # Truncate callback_data to 64 bytes (Telegram limit)
        if len(accept_all_tool_data.encode()) <= 64:
            session_row.append(InlineKeyboardButton(
                f"Accept all {tool_name}", callback_data=accept_all_tool_data,
            ))

    rows = [primary_row]
    if session_row:
        rows.append(session_row)
    keyboard = InlineKeyboardMarkup(rows)

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
        **thread_kwargs,
    )

    # Create a future and wait for the callback
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    if tool_name in ("Edit", "Write"):
        _approval_futures[f"accept_all_edits:{tool_use_id}"] = future
    accept_all_tool_key = f"accept_all_tool:{tool_use_id}:{tool_name}"
    if tool_name not in ("Edit", "Write") and len(accept_all_tool_key.encode()) <= 64:
        _approval_futures[accept_all_tool_key] = future
    # Register prefix-specific key for Bash.
    accept_prefix_key = ""
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        prefix = _extract_bash_prefix(command)
        if prefix:
            accept_prefix_key = f"accept_bash_pfx:{tool_use_id}:{prefix}"
            if len(accept_prefix_key.encode()) <= 64:
                _approval_futures[accept_prefix_key] = future

    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_futures.pop(f"accept_all_edits:{tool_use_id}", None)
        _approval_futures.pop(accept_all_tool_key, None)
        if accept_prefix_key:
            _approval_futures.pop(accept_prefix_key, None)
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

    Handles: approve:*, deny:*, show_prompt:*, show_bash:*,
    accept_all_edits:*, accept_bash_pfx:*, accept_all_tool:*.
    Returns True if the callback was handled.
    """
    import aiosqlite

    from open_udang.db import ChatScope
    from open_udang.handlers.state import _edit_approved_sessions
    from open_udang.handlers.utils import _get_context, chat_scope_from_message
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
        if query.message:
            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(scope, config, db)
            _edit_approved_sessions.add((scope, ctx_name))
            logger.info(
                "Accept-all-edits enabled for scope %s context %s",
                scope,
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

    # Handle "Accept all <prefix>" for Bash commands — approve this tool and
    # enable auto-approval for future Bash commands matching "<prefix> *".
    if data.startswith("accept_bash_pfx:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Parse: "accept_bash_pfx:<id>:<prefix>"
        parts = data.split(":", 2)
        prefix = parts[2] if len(parts) >= 3 else ""

        if query.message and prefix:
            from open_udang.handlers.state import _tool_approved_sessions

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(scope, config, db)
            rule = ApprovalRule(tool_name="Bash", pattern=f"{prefix} *")
            _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)
            logger.info(
                "Accept-all-Bash(%s *) enabled for scope %s context %s",
                prefix,
                scope,
                ctx_name,
            )

        future.set_result(True)
        escaped_prefix = _escape_mdv2(prefix)
        await query.answer(
            f"Approved. Future {prefix} commands auto-approved."
        )

        if query.message:
            try:
                icon = '\u2705'
                compact = (
                    f"{icon} *Bash* \u2014 Approved\\. "
                    f"_Future {escaped_prefix} commands auto\\-approved\\._"
                )
                await query.message.edit_text(
                    text=compact,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")
        return True

    # Handle "Accept all <tool>" -- approve this tool and enable auto-approval
    # for all future uses of that specific tool for this session.
    if data.startswith("accept_all_tool:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Parse tool name from callback data: "accept_all_tool:<id>:<tool_name>"
        parts = data.split(":", 2)
        accepted_tool_name = parts[2] if len(parts) >= 3 else ""

        # Determine the chat's active context to scope the flag
        if query.message and accepted_tool_name:
            from open_udang.handlers.state import _tool_approved_sessions

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(scope, config, db)
            rule = ApprovalRule(tool_name=accepted_tool_name, pattern=None)
            _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)
            logger.info(
                "Accept-all-%s enabled for scope %s context %s",
                accepted_tool_name,
                scope,
                ctx_name,
            )

        future.set_result(True)
        escaped_tool = _escape_mdv2(accepted_tool_name)
        await query.answer(
            f"Approved. All future {accepted_tool_name} calls will be auto-approved."
        )

        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                status = (
                    f"\n\n\u2705 *Approved\\.* _All future {escaped_tool} "
                    f"calls auto\\-approved\\._"
                )
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
