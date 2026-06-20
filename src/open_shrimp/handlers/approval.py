"""Tool approval via Telegram inline keyboards.

The orchestration here (sending the keyboard, awaiting the future,
resolving the per-callback actions, editing the message on resolution)
is backend-agnostic.  The per-tool text and per-tool keyboard buttons
come from the active backend's ``BackendPolicy`` — see
``backend/policy.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.db import ChatScope

from open_shrimp.handlers.state import (
    _approval_futures,
    _approval_metadata,
    _approval_tool_names,
    _pending_agent_inputs,
    _pending_session_dirs,
    _pending_tool_approvals,
)
from open_shrimp.handlers.utils import _escape_mdv2
from open_shrimp.hooks import ApprovalRule, HostBashOutcome
from open_shrimp.sudo_audit import log_sudo

if TYPE_CHECKING:
    from open_shrimp.backend.policy import BackendPolicy

logger = logging.getLogger(__name__)


def _resolve_policy(policy: "BackendPolicy | None") -> "BackendPolicy":
    if policy is not None:
        return policy
    from open_shrimp.client_manager import resolve_backend

    return resolve_backend(None).policy


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
    policy: "BackendPolicy | None" = None,
) -> None:
    """Send a read-only diff message for an auto-approved edit."""
    p = _resolve_policy(policy)
    text = p.format_auto_approved_diff(tool_name, tool_input, cwd)
    text += "\n✅ _Auto\\-approved_"

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
    base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    bot_token: str = "",
    suggested_session_dir: str | None = None,
    scope: ChatScope | None = None,
    context_name: str | None = None,
    policy: "BackendPolicy | None" = None,
) -> bool:
    """Send an inline keyboard for tool approval and wait for response.

    When ``suggested_session_dir`` is set (the file tool's target is
    outside the approved directories), an extra "Allow <dir>/ this
    session" button is added that, when clicked, adds the directory to
    the session-approved set so subsequent tool calls in that directory
    auto-approve.  ``scope`` and ``context_name`` are required to scope
    that approval state.
    """
    p = _resolve_policy(policy)
    text = p.format_approval_text(tool_name, tool_input, cwd)

    approve_data = f"approve:{tool_use_id}"
    deny_data = f"deny:{tool_use_id}"
    _approval_tool_names[tool_use_id] = tool_name
    _approval_metadata[tool_use_id] = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "chat_id": chat_id,
    }

    extras = p.approval_keyboard_extras(
        tool_name,
        tool_input,
        tool_use_id,
        base_url,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        bot_token=bot_token,
        is_private_chat=is_private_chat,
    )

    # Primary row: optional policy-supplied extras (e.g. Agent "Show
    # prompt"), then the standard [Approve][Deny] pair.
    primary_row: list[InlineKeyboardButton] = list(extras.primary_row_extras)
    primary_row.append(InlineKeyboardButton("Approve", callback_data=approve_data))
    primary_row.append(InlineKeyboardButton("Deny", callback_data=deny_data))

    # Session-scoped row from the policy plus the orchestration-owned
    # blanket-accept and dir-scoped buttons.
    session_row: list[InlineKeyboardButton] = list(extras.session_row)

    accept_all_tool_key = ""
    accept_all_tool_data = ""
    if extras.use_blanket_accept_all:
        accept_all_tool_key = uuid.uuid4().hex[:12]
        _pending_tool_approvals[accept_all_tool_key] = tool_name
        accept_all_tool_data = f"accept_all_tool:{accept_all_tool_key}"
        session_row.append(InlineKeyboardButton(
            f"Accept all {tool_name}", callback_data=accept_all_tool_data,
        ))

    # Out-of-scope file access: offer to approve the entire directory
    # for the rest of the session.
    accept_dir_data = ""
    accept_dir_key = ""
    if suggested_session_dir and scope is not None and context_name is not None:
        accept_dir_key = uuid.uuid4().hex[:12]
        _pending_session_dirs[accept_dir_key] = (
            scope, context_name, suggested_session_dir,
        )
        accept_dir_data = f"accept_dir:{tool_use_id}:{accept_dir_key}"
        if len(accept_dir_data.encode()) <= 64:
            dir_label = os.path.basename(
                suggested_session_dir.rstrip(os.sep),
            ) or suggested_session_dir
            if len(dir_label) > 24:
                dir_label = "…" + dir_label[-23:]
            if p.is_mutating(tool_name):
                btn_label = f"Allow all edits in {dir_label}/"
            else:
                btn_label = f"Allow reading from {dir_label}/"
            session_row.append(InlineKeyboardButton(
                btn_label, callback_data=accept_dir_data,
            ))
        else:
            _pending_session_dirs.pop(accept_dir_key, None)
            accept_dir_data = ""
            accept_dir_key = ""

    rows: list[list[InlineKeyboardButton]] = []
    rows.extend(extras.pre_primary_rows)
    rows.append(primary_row)
    if session_row:
        rows.append(session_row)
    keyboard = InlineKeyboardMarkup(rows)

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
        **thread_kwargs,
    )
    _approval_metadata[tool_use_id]["message_id"] = sent_msg.message_id

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    for cb_data in extras.future_callback_data:
        _approval_futures[cb_data] = future
    if accept_all_tool_data:
        _approval_futures[accept_all_tool_data] = future
    if accept_dir_data:
        _approval_futures[accept_dir_data] = future

    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        for cb_data in extras.future_callback_data:
            _approval_futures.pop(cb_data, None)
        if accept_all_tool_data:
            _approval_futures.pop(accept_all_tool_data, None)
        if accept_all_tool_key:
            _pending_tool_approvals.pop(accept_all_tool_key, None)
        if accept_dir_data:
            _approval_futures.pop(accept_dir_data, None)
            _pending_session_dirs.pop(accept_dir_key, None)
        _pending_agent_inputs.pop(tool_use_id, None)
        _approval_tool_names.pop(tool_use_id, None)
        _approval_metadata.pop(tool_use_id, None)


# ---------------------------------------------------------------------------
# host_bash (sudo mode) approval — dedicated flow with 10s auto-deny + live
# countdown. Uses its own callback prefixes (hb_approve:/hb_deny:) so the
# standard approve/deny handler doesn't fight with the countdown task over
# message edits.
# ---------------------------------------------------------------------------


_HOST_BASH_TIMEOUT_SECONDS = 10.0
_HOST_BASH_TICK_SECONDS = 2.0
_HOST_BASH_APPROVE_PREFIX = "hb_approve:"
_HOST_BASH_DENY_PREFIX = "hb_deny:"


def _render_command_block(command: str, max_len: int) -> str:
    """Render a bash command as a MarkdownV2 code block with truncation."""
    shown = command
    if len(shown) > max_len:
        shown = shown[:max_len] + "\n..."
    return f"```bash\n{_escape_mdv2(shown)}\n```"


def _format_host_bash_approval(
    tool_input: dict[str, Any], remaining: float,
) -> str:
    """Render the host_bash approval prompt with a countdown line."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    cwd = tool_input.get("cwd", "")

    header = "⚠️ *HOST shell* \\(sudo mode\\)"
    parts: list[str] = [header]
    if description:
        parts.append(_escape_mdv2(description))
    parts.append(_render_command_block(command, 4096 - 400))
    if cwd:
        parts.append(f"_cwd:_ `{_escape_mdv2(cwd)}`")
    secs = max(0, int(round(remaining)))
    parts.append(
        f"_Auto\\-deny in {secs}s — this command runs OUTSIDE the "
        f"sandbox\\._"
    )
    return "\n\n".join(parts)


def _format_host_bash_final(
    tool_input: dict[str, Any], outcome: HostBashOutcome,
) -> str:
    """Render the final state of the host_bash approval message."""
    icon = {
        "approved": "✅",
        "denied": "❌",
        "timeout": "⏱️",
    }[outcome]
    verb = {
        "approved": "Approved",
        "denied": "Denied",
        "timeout": "Auto\\-denied \\(no response within 10s\\)",
    }[outcome]
    block = _render_command_block(tool_input.get("command", ""), 4096 - 200)
    return f"{icon} *HOST shell* — {verb}\n\n{block}"


async def _host_bash_countdown(
    bot: Bot,
    chat_id: int,
    message_id: int,
    tool_use_id: str,
    tool_input: dict[str, Any],
    deadline: float,
    future: asyncio.Future[bool],
) -> None:
    """Edit the approval message every tick with the remaining countdown."""
    loop = asyncio.get_running_loop()
    last_secs = int(round(_HOST_BASH_TIMEOUT_SECONDS))
    while True:
        try:
            await asyncio.wait_for(
                asyncio.shield(future), timeout=_HOST_BASH_TICK_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        if future.done():
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        secs = max(0, int(round(remaining)))
        if secs == last_secs:
            continue
        last_secs = secs
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_format_host_bash_approval(tool_input, remaining),
                parse_mode="MarkdownV2",
                reply_markup=_host_bash_keyboard(tool_use_id),
            )
        except Exception:
            pass


def _host_bash_keyboard(tool_use_id: str) -> InlineKeyboardMarkup:
    """Build the two-button [Approve] [Deny] keyboard for host_bash."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Approve",
            callback_data=f"{_HOST_BASH_APPROVE_PREFIX}{tool_use_id}",
        ),
        InlineKeyboardButton(
            "Deny",
            callback_data=f"{_HOST_BASH_DENY_PREFIX}{tool_use_id}",
        ),
    ]])


async def _send_host_bash_approval(
    bot: Bot,
    chat_id: int,
    context_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    thread_id: int | None = None,
) -> HostBashOutcome:
    """Send a host_bash approval prompt and resolve to approved/denied/timeout."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    timed_out = [False]

    def _auto_deny() -> None:
        if not future.done():
            timed_out[0] = True
            future.set_result(False)

    timer = loop.call_later(_HOST_BASH_TIMEOUT_SECONDS, _auto_deny)
    deadline = loop.time() + _HOST_BASH_TIMEOUT_SECONDS

    approve_data = f"{_HOST_BASH_APPROVE_PREFIX}{tool_use_id}"
    deny_data = f"{_HOST_BASH_DENY_PREFIX}{tool_use_id}"
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    # The exact wire name for host_bash is per-backend; the callback
    # handler only needs an opaque marker to match the right entry.
    host_bash_marker = "host_bash"
    _approval_tool_names[tool_use_id] = host_bash_marker
    _approval_metadata[tool_use_id] = {
        "tool_name": host_bash_marker,
        "tool_input": tool_input,
        "chat_id": chat_id,
    }

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=_format_host_bash_approval(tool_input, _HOST_BASH_TIMEOUT_SECONDS),
        parse_mode="MarkdownV2",
        reply_markup=_host_bash_keyboard(tool_use_id),
        **thread_kwargs,
    )
    message_id = sent_msg.message_id
    _approval_metadata[tool_use_id]["message_id"] = message_id

    countdown_task = asyncio.create_task(_host_bash_countdown(
        bot, chat_id, message_id, tool_use_id, tool_input, deadline, future,
    ))

    try:
        approved = await future
    finally:
        timer.cancel()
        countdown_task.cancel()
        try:
            await countdown_task
        except (asyncio.CancelledError, Exception):
            pass
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_tool_names.pop(tool_use_id, None)
        _approval_metadata.pop(tool_use_id, None)

    if timed_out[0]:
        outcome: HostBashOutcome = "timeout"
    elif approved:
        outcome = "approved"
    else:
        outcome = "denied"

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_format_host_bash_final(tool_input, outcome),
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception:
        logger.debug(
            "Failed to edit host_bash approval message", exc_info=True,
        )

    await log_sudo(
        chat_id=chat_id,
        context_name=context_name,
        command=tool_input.get("command", ""),
        outcome=outcome,
    )
    return outcome


# ---------------------------------------------------------------------------
# Auto-resolve parallel pending approvals after "accept all" actions
# ---------------------------------------------------------------------------


async def _auto_resolve_pending_approvals(
    bot: Bot,
    rule: ApprovalRule | None,
    is_edit_rule: bool,
    chat_id: int,
    approved_dir: str | None = None,
    policy: "BackendPolicy | None" = None,
) -> None:
    """Resolve all pending approval futures that match a newly created rule."""
    from open_shrimp.hooks import matches_approval_rule, tool_path_within_dir

    p = _resolve_policy(policy)

    for tool_use_id, meta in list(_approval_metadata.items()):
        if meta.get("chat_id") != chat_id:
            continue

        t_name = meta["tool_name"]
        t_input = meta["tool_input"]
        msg_id = meta.get("message_id")

        matched = False
        if is_edit_rule and p.is_mutating(t_name):
            matched = True
        elif rule is not None and matches_approval_rule(rule, t_name, t_input):
            matched = True
        elif approved_dir is not None and tool_path_within_dir(
            t_name, t_input, approved_dir, policy=p,
        ):
            matched = True

        if not matched:
            continue

        approve_key = f"approve:{tool_use_id}"
        future = _approval_futures.get(approve_key)
        if future is None or future.done():
            continue

        future.set_result(True)
        logger.info(
            "Auto-resolved pending approval for %s (tool_use_id=%s)",
            t_name,
            tool_use_id,
        )

        if msg_id:
            try:
                escaped_tool = _escape_mdv2(t_name)
                icon = '✅'
                compact = f"{icon} *{escaped_tool}* — Auto\\-approved\\."
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=compact,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception(
                    "Failed to edit auto-resolved approval message"
                )


# ---------------------------------------------------------------------------
# Callback query handling for approval-related buttons
# ---------------------------------------------------------------------------


async def handle_approval_callback(
    query: Any,
    data: str,
    config: Any,
    context: Any,
) -> bool:
    """Handle approval-related callback queries."""
    import aiosqlite

    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import _edit_approved_sessions
    from open_shrimp.handlers.utils import _get_context, chat_scope_from_message
    from open_shrimp.stream import _bash_output_store

    p = _resolve_policy(None)

    # Handle "Show prompt" expansion for Agent-like tools.
    if data.startswith("show_prompt:"):
        tool_use_id = data[len("show_prompt:"):]
        tool_input = _pending_agent_inputs.get(tool_use_id)
        if not tool_input:
            await query.answer("Prompt data no longer available.")
            return True

        await query.answer()

        if query.message:
            tool_name = _approval_tool_names.get(tool_use_id, "")
            expanded_text = p.format_expanded_prompt(tool_name, tool_input)
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
            from open_shrimp.markdown import gfm_to_telegram

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
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to remove bash button")
        return True

    # Handle "Accept all edits" -- approve this tool and enable auto-
    # approval for all future mutating-file tools within cwd this session.
    if data.startswith("accept_all_edits:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

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
                status = "\n\n✅ *Approved\\.* _All future edits auto\\-approved\\._"
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

        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=None, is_edit_rule=True, chat_id=chat_id,
                policy=p,
            )
        return True

    # Handle "Allow & remember: <prefix> *" for Bash commands.
    if data.startswith("accept_bash_pfx:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        parts = data.split(":", 2)
        prefix = parts[2] if len(parts) >= 3 else ""

        if query.message and prefix:
            from open_shrimp.handlers.state import _tool_approved_sessions
            from open_shrimp.settings_local import save_persistent_rule

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, ctx_config = await _get_context(scope, config, db)
            rule = ApprovalRule(tool_name="Bash", pattern=f"{prefix} *")
            _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)

            try:
                persisted = await save_persistent_rule(ctx_config.directory, rule)
            except OSError:
                logger.exception("Failed to persist rule to settings.local.json")
                persisted = False

            logger.info(
                "Saved persistent Bash(%s:*) rule for scope %s context %s (persisted=%s)",
                prefix,
                scope,
                ctx_name,
                persisted,
            )

        future.set_result(True)
        escaped_prefix = _escape_mdv2(prefix)
        await query.answer(
            f"Approved. Rule saved: {prefix} * auto-approved."
        )

        if query.message:
            try:
                icon = '✅'
                compact = (
                    f"{icon} *Bash* — Approved\\. "
                    f"_Rule saved: {escaped_prefix} \\* auto\\-approved\\._"
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

        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None and prefix:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=rule, is_edit_rule=False, chat_id=chat_id,
                policy=p,
            )
        return True

    # Handle "Allow <reading from|all edits in> <dir>/ this session".
    if data.startswith("accept_dir:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        parts = data.split(":", 2)
        short_key = parts[2] if len(parts) >= 3 else ""

        from open_shrimp.handlers.state import (
            _pending_session_dirs,
            _session_approved_dirs,
        )

        pending = _pending_session_dirs.pop(short_key, None)
        if pending is None:
            await query.answer("This action has expired.")
            return True

        scope, ctx_name, directory = pending
        _session_approved_dirs.setdefault((scope, ctx_name), set()).add(
            directory,
        )
        logger.info(
            "Session-approved dir %s for scope %s context %s",
            directory,
            scope,
            ctx_name,
        )

        future.set_result(True)
        escaped_dir = _escape_mdv2(directory)
        await query.answer(
            f"Approved. {directory}/ allowed for this session."
        )

        if query.message:
            try:
                original_md = (
                    query.message.text_markdown_v2
                    or query.message.text
                    or ""
                )
                status = (
                    f"\n\n✅ *Approved\\.* "
                    f"_All future tool calls in `{escaped_dir}` "
                    f"auto\\-approved this session\\._"
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

        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None:
            await _auto_resolve_pending_approvals(
                query.get_bot(),
                rule=None,
                is_edit_rule=False,
                chat_id=chat_id,
                approved_dir=directory,
                policy=p,
            )
        return True

    # Handle "Accept all <tool>".
    if data.startswith("accept_all_tool:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        token = data.split(":", 1)[1]
        accepted_tool_name = _pending_tool_approvals.pop(token, "")

        if query.message and accepted_tool_name:
            from open_shrimp.handlers.state import _tool_approved_sessions

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
                    f"\n\n✅ *Approved\\.* _All future {escaped_tool} "
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

        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None and accepted_tool_name:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=rule, is_edit_rule=False, chat_id=chat_id,
                policy=p,
            )
        return True

    # Handle host_bash (sudo mode) approve/deny.
    if data.startswith(_HOST_BASH_APPROVE_PREFIX) or data.startswith(
        _HOST_BASH_DENY_PREFIX,
    ):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True
        approved = data.startswith(_HOST_BASH_APPROVE_PREFIX)
        future.set_result(approved)
        await query.answer("Approved." if approved else "Denied.")
        return True

    # Handle approve/deny
    if data.startswith("approve:") or data.startswith("deny:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        approved = data.startswith("approve:")
        future.set_result(approved)

        tool_use_id = data.split(":", 1)[1] if ":" in data else ""
        tool_name = _approval_tool_names.get(tool_use_id, "")

        action = "Approved" if approved else "Denied"
        await query.answer(f"{action}.")

        # Update the message to show the decision (remove buttons, append
        # status).  For Bash-like tools, collapse to a compact one-liner
        # since the "Show output" button message that follows will show
        # the command again.
        if query.message:
            try:
                if tool_name and p.is_bash_like(tool_name):
                    icon = '✅' if approved else '❌'
                    compact = f"{icon} *{_escape_mdv2(tool_name)}* — {action}\\."
                    await query.message.edit_text(
                        text=compact,
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                else:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    icon = '✅' if approved else '❌'
                    status = f"\n\n{icon} *{action}\\.*"
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

    return False
