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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.cross_context import handle_handoff_callback
from open_shrimp.db import ChatScope

from open_shrimp.handlers.state import (
    CONFIG_WRITE_APPROVE_PREFIX as _CONFIG_WRITE_APPROVE_PREFIX,
    HOST_BASH_APPROVE_PREFIX as _HOST_BASH_APPROVE_PREFIX,
    _approval_futures,
    _approval_metadata,
    _approval_resolved_via,
    _approval_tool_names,
    _pending_agent_inputs,
    _pending_session_dirs,
    _pending_tool_approvals,
    register_pending_approval,
    release_pending_approval,
    RESOLVED_VIA_ANDROID,
    take_pending_approvals,
)
from open_shrimp.markdown import (
    RICH_MAX_BODY,
    escape_rich,
    escape_rich_inline,
    rich_code_block,
    rich_details,
)
from open_shrimp.rich_message import (
    body_of,
    edit_message_rich,
    edit_rich,
    send_rich,
)
from open_shrimp.hooks import ApprovalRule, HostBashOutcome
from open_shrimp.sudo_audit import log_sudo

if TYPE_CHECKING:
    from open_shrimp.backend.policy import BackendPolicy

logger = logging.getLogger(__name__)


def _resolve_policy(
    policy: "BackendPolicy | None",
    scope: ChatScope | None = None,
) -> "BackendPolicy":
    if policy is not None:
        return policy
    from open_shrimp.client_manager import resolve_backend

    return resolve_backend(scope=scope).policy


# ---------------------------------------------------------------------------
# Approval keyboard & auto-approved diff notification
# ---------------------------------------------------------------------------


async def resolve_approval_from_device(
    tool_use_id: str, approve: bool,
) -> bool:
    """Resolve a pending approval on behalf of the Android companion.

    Returns ``False`` when the card is already gone — answered in Telegram
    first, or the turn ended under it.

    Each card sender registers one future under its own approve and deny
    callback keys, and the phone knows the ``tool_use_id`` and nothing else,
    so every prefix is probed rather than named: a sender missing from
    ``APPROVE_CALLBACK_PREFIXES`` is one the phone can display and never
    answer.  Lives here, with the senders and the key format, so the HTTP
    layer does not have to know either.
    """
    from open_shrimp.handlers.state import (
        APPROVE_CALLBACK_PREFIXES,
        RESOLVED_VIA_ANDROID,
        STANDARD_APPROVE_PREFIX,
        _approval_futures,
        _approval_resolved_via,
    )

    for prefix in APPROVE_CALLBACK_PREFIXES:
        future = _approval_futures.get(f"{prefix}{tool_use_id}")
        if future is None:
            continue
        if future.done():
            return False
        # The host-escape and config-write flows edit their own message
        # unconditionally and never read ``_approval_resolved_via``, so only
        # the standard path needs the "resolved on phone" marker.
        if prefix == STANDARD_APPROVE_PREFIX:
            _approval_resolved_via[tool_use_id] = RESOLVED_VIA_ANDROID
        future.set_result(approve)
        return True
    return False


async def _close_card(
    bot: Bot, chat_id: int, message_id: int, text: str,
) -> None:
    """Replace a card's body with its outcome and take the buttons away.

    A card whose buttons outlive its decision invites a tap that resolves
    nothing.  Falling back to stripping the markup alone matters when the
    replacement text is what Telegram refused: the buttons still have to
    go, or the card stays live-looking forever.
    """
    try:
        await edit_rich(bot, chat_id, message_id, text, reply_markup=None)
    except Exception:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None,
            )
        except Exception:
            logger.debug("Failed to close an approval card", exc_info=True)


async def _send_auto_approved_diff(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str | None = None,
    thread_id: int | None = None,
    policy: "BackendPolicy | None" = None,
    scope: ChatScope | None = None,
) -> None:
    """Send a read-only diff message for an auto-approved edit.

    Folded shut: nobody is being asked to decide, so the change costs one row
    in the transcript and opens on a tap when someone wants to read it.
    """
    p = _resolve_policy(policy, scope=scope)
    body = p.format_auto_approved_diff(tool_name, tool_input, cwd)
    # A card is a header line and a fenced patch, and the fence is what makes
    # the split safe to do here: a renderer that emits neither — the generic
    # one joins its rows with <br> — has nothing to fold, and folding on the
    # paragraph break alone would put its whole payload in the summary row.
    summary, fence, patch = body.partition("\n\n```")
    text = (
        rich_details(f"✅ {summary}", fence.lstrip("\n") + patch) if fence
        else f"✅ {body}"
    )

    try:
        await send_rich(
            bot, chat_id, text,
            thread_id=thread_id,
            disable_notification=True,
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
    provenance: str | None = None,
) -> bool:
    """Send an inline keyboard for tool approval and wait for response.

    When ``suggested_session_dir`` is set (the file tool's target is
    outside the approved directories), an extra "Allow <dir>/ this
    session" button is added that, when clicked, adds the directory to
    the session-approved set so subsequent tool calls in that directory
    auto-approve.  ``scope`` and ``context_name`` are required to scope
    that approval state.

    ``provenance`` is an optional, already-escaped rich header
    prepended to the prompt text (used by ``ask_context`` to show that an
    approval originates from a cross-context sub-query rather than the
    active conversation).
    """
    p = _resolve_policy(policy, scope=scope)
    text = p.format_approval_text(tool_name, tool_input, cwd)
    if provenance:
        text = f"{provenance}\n\n{text}"

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

    sent_msg = await send_rich(
        bot, chat_id, text, thread_id=thread_id, reply_markup=keyboard,
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
    pending = register_pending_approval(
        scope, chat_id, sent_msg.message_id, future, bot=bot, text=text,
    )

    try:
        result = await future
        # If the decision came from the Android companion's notification
        # action (not a Telegram CallbackQuery), the Telegram message still
        # shows its live buttons — collapse it here to mirror the inline path.
        if _approval_resolved_via.pop(tool_use_id, None) == RESOLVED_VIA_ANDROID:
            icon = "✅" if result else "❌"
            action = "Approved on phone" if result else "Denied on phone"
            try:
                await edit_rich(
                    bot, chat_id, sent_msg.message_id,
                    f"{text}\n\n{icon} *{escape_rich_inline(action)}.*",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=sent_msg.message_id,
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("Failed to edit phone-resolved approval message")
        return result
    finally:
        release_pending_approval(scope, pending)
        _approval_resolved_via.pop(tool_use_id, None)
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


async def retire_pending_approvals(scope: ChatScope) -> None:
    """Invalidate every live approval card for *scope*.

    A card outliving its session still shows live buttons that nothing is
    listening to, so the tap neither acts nor reports.  Strip the buttons,
    say so on the card, and cancel the future to unblock any waiter.
    """
    for entry in take_pending_approvals(scope):
        if not entry.future.done():
            entry.future.cancel()
        if entry.bot is None or entry.message_id is None:
            continue
        try:
            await edit_rich(
                entry.bot, entry.chat_id, entry.message_id,
                f"{entry.text}\n\n⚪️ *Session ended — no longer live.*",
                reply_markup=None,
            )
        except Exception:
            try:
                await entry.bot.edit_message_reply_markup(
                    chat_id=entry.chat_id,
                    message_id=entry.message_id,
                    reply_markup=None,
                )
            except Exception:
                logger.debug("Failed to retire approval card", exc_info=True)


# ---------------------------------------------------------------------------
# host_bash (sudo mode) approval — dedicated flow with 30s auto-deny + live
# countdown. Uses its own callback prefixes (hb_approve:/hb_deny:) so the
# standard approve/deny handler doesn't fight with the countdown task over
# message edits.
# ---------------------------------------------------------------------------


_HOST_BASH_TIMEOUT_SECONDS = 30.0
_HOST_BASH_TICK_SECONDS = 2.0
_HOST_BASH_DENY_PREFIX = "hb_deny:"


def _render_command_block(command: str, max_len: int) -> str:
    """Render a bash command as a highlighted code block with truncation."""
    shown = command
    if len(shown) > max_len:
        shown = shown[:max_len] + "\n..."
    return rich_code_block(shown, "bash")


def _format_host_bash_approval(
    tool_input: dict[str, Any], remaining: float, is_monitor: bool = False,
) -> str:
    """Render the host-escape approval prompt with a countdown line."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    cwd = tool_input.get("cwd", "")

    header = (
        "⚠️ **Start HOST monitor** (sudo mode)"
        if is_monitor
        else "⚠️ **HOST shell** (sudo mode)"
    )
    parts: list[str] = [header]
    if description:
        parts.append(escape_rich(description))
    parts.append(_render_command_block(command, RICH_MAX_BODY))
    if cwd:
        parts.append(f"*cwd:* `{cwd}`")
    secs = max(0, int(round(remaining)))
    if is_monitor:
        parts.append(
            f"<blockquote>Auto-deny in {secs}s — this streams each output "
            f"line as an event and runs OUTSIDE the sandbox."
            f"<cite>host_monitor</cite></blockquote>"
        )
    else:
        parts.append(
            f"<blockquote>Auto-deny in {secs}s — this command runs OUTSIDE "
            f"the sandbox.<cite>host_bash</cite></blockquote>"
        )
    return "\n\n".join(parts)


def _format_host_bash_final(
    tool_input: dict[str, Any],
    outcome: HostBashOutcome,
    is_monitor: bool = False,
) -> str:
    """Render the final state of the host-escape approval message.

    One row once the decision is made, with the command folded away: an
    approved command is about to be repeated by its own card, and the row is
    what the user scrolls past on the way to the answer.
    """
    icon = {
        "approved": "✅",
        "denied": "❌",
        "timeout": "⏱️",
    }[outcome]
    verb = {
        "approved": "Approved",
        "denied": "Denied",
        "timeout": "Auto-denied (no response within 30s)",
    }[outcome]
    label = "HOST monitor" if is_monitor else "HOST shell"
    description = (tool_input.get("description") or "").strip()
    summary = f"{icon} **{label}** — {verb}"
    if description:
        summary += f" · {escape_rich_inline(description)}"
    block = _render_command_block(tool_input.get("command", ""), RICH_MAX_BODY)
    return rich_details(summary, block)


async def _host_bash_countdown(
    bot: Bot,
    chat_id: int,
    message_id: int,
    tool_use_id: str,
    tool_input: dict[str, Any],
    deadline: float,
    future: asyncio.Future[bool],
    is_monitor: bool = False,
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
            await edit_rich(
                bot, chat_id, message_id,
                _format_host_bash_approval(tool_input, remaining, is_monitor),
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
    is_monitor: bool = False,
) -> HostBashOutcome:
    """Send a host-escape approval prompt and resolve to approved/denied/timeout."""
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

    sent_msg = await send_rich(
        bot, chat_id,
        _format_host_bash_approval(
            tool_input, _HOST_BASH_TIMEOUT_SECONDS, is_monitor,
        ),
        thread_id=thread_id,
        reply_markup=_host_bash_keyboard(tool_use_id),
    )
    message_id = sent_msg.message_id
    _approval_metadata[tool_use_id]["message_id"] = message_id

    countdown_task = asyncio.create_task(_host_bash_countdown(
        bot, chat_id, message_id, tool_use_id, tool_input, deadline, future,
        is_monitor,
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
        await edit_rich(
            bot, chat_id, message_id,
            _format_host_bash_final(tool_input, outcome, is_monitor),
            reply_markup=None,
        )
    except Exception:
        logger.debug(
            "Failed to edit host-escape approval message", exc_info=True,
        )

    await log_sudo(
        chat_id=chat_id,
        context_name=context_name,
        command=tool_input.get("command", ""),
        outcome=outcome,
    )
    return outcome


# ---------------------------------------------------------------------------
# Config-write approval — the supervisor's write_context / remove_context.
#
# Its own callback prefixes, and no "accept all" button of any kind: the
# Edit/Write session grant deliberately does not reach config.yaml, and a
# card that offered to stop asking would put it back.  No auto-deny timer
# either — like every other approval here, an untapped card waits.
# ---------------------------------------------------------------------------


_CONFIG_WRITE_DENY_PREFIX = "cw_deny:"


def _config_write_keyboard(tool_use_id: str) -> InlineKeyboardMarkup:
    """[Approve] [Deny], and nothing that grants anything beyond this call."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Approve",
            callback_data=f"{_CONFIG_WRITE_APPROVE_PREFIX}{tool_use_id}",
        ),
        InlineKeyboardButton(
            "Deny",
            callback_data=f"{_CONFIG_WRITE_DENY_PREFIX}{tool_use_id}",
        ),
    ]])


def _format_config_write(headline: str, diff: str, note: str = "") -> str:
    """The card: what it does in a sentence, then the diff that proves it.

    The diff goes in a fence, which a rich message takes literally — a diff
    is almost entirely the characters an escaper would touch, and escaping
    them here would print the backslashes.
    """
    parts = [
        f"⚙️ **Change OpenShrimp's configuration**\n\n"
        f"{escape_rich(headline)}."
    ]
    if note:
        parts.append(escape_rich(note))
    parts.append(rich_code_block(diff, "diff"))
    return "\n\n".join(parts)


async def _send_config_write_approval(
    bot: Bot,
    chat_id: int,
    config_path: str,
    tool: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    thread_id: int | None = None,
    scope: ChatScope | None = None,
) -> str | None:
    """Show what the write would do and wait; None once the user approves.

    The plan is built before anything is sent, so a write that could
    never happen — a stale state token, a withheld field, a change that
    would not validate — is refused to the model without troubling the
    user with a card they would only have to deny.

    The state token is checked again on approval, because a card waits
    without a deadline and the user can edit the same file in the config
    Mini App while it sits there.  On a mismatch the card is re-rendered
    with the diff the request would produce against the file as it now
    stands, and nothing is written: what the user reads and what lands on
    disk are the same bytes or there is no write.
    """
    from open_shrimp.supervisor_write import (
        ConfigWriteRefused,
        plan_config_write,
    )

    path = Path(config_path)
    try:
        plan = plan_config_write(path, tool, tool_input)
    except ConfigWriteRefused as exc:
        return str(exc)

    approve_data = f"{_CONFIG_WRITE_APPROVE_PREFIX}{tool_use_id}"
    deny_data = f"{_CONFIG_WRITE_DENY_PREFIX}{tool_use_id}"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    # Deliberately not registered in ``_approval_metadata``: that is what
    # ``_auto_resolve_pending_approvals`` walks when the user grants "accept
    # all edits", and a config write must never be answered by a grant given
    # to something else.

    text = _format_config_write(plan.headline, plan.diff)
    sent_msg = await send_rich(
        bot, chat_id, text,
        thread_id=thread_id,
        reply_markup=_config_write_keyboard(tool_use_id),
    )
    pending = register_pending_approval(
        scope, chat_id, sent_msg.message_id, future, bot=bot, text=text,
    )

    try:
        approved = await future
    finally:
        release_pending_approval(scope, pending)
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)

    if not approved:
        await _close_card(
            bot, chat_id, sent_msg.message_id, f"{text}\n\n❌ *Denied\\.*",
        )
        return (
            "The user denied that change, so config.yaml is unchanged. Ask "
            "them what they would rather do; do not propose the same change "
            "again unhandled."
        )

    # Ask the planner the same question a second time rather than
    # comparing tokens here: it enforces the token the model supplied, so
    # succeeding *is* the answer to "is this still the file I rendered",
    # and it is the one place that knows every other way a write can stop
    # being possible while a card waits.
    try:
        plan_config_write(path, tool, tool_input)
    except ConfigWriteRefused as exc:
        stale = str(exc)
    else:
        await _close_card(
            bot, chat_id, sent_msg.message_id, f"{text}\n\n✅ *Approved\\.*",
        )
        return None

    # The file moved under the card.  Show what the same request would do
    # now — the user is owed a sight of it — and write nothing.
    note = (
        "config.yaml changed while this was waiting, so nothing was "
        "written. Here is what this request would do to the file as it "
        "now stands."
    )
    try:
        restated = plan_config_write(path, tool, tool_input, enforce_token=False)
        replacement = _format_config_write(restated.headline, restated.diff, note)
    except ConfigWriteRefused as exc:
        replacement = _format_config_write(
            plan.headline, plan.diff, f"{note} It would now be refused: {exc}",
        )
    await _close_card(
        bot, chat_id, sent_msg.message_id, replacement,
    )
    return (
        f"{stale} The user approved a diff that no longer describes the file, "
        f"so the approval was spent and nothing was written. Tell them what "
        f"changed before proposing it again."
    )


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
    scope: ChatScope | None = None,
) -> None:
    """Resolve all pending approval futures that match a newly created rule."""
    from open_shrimp.hooks import matches_approval_rule, tool_path_within_dir

    p = _resolve_policy(policy, scope=scope)

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
                compact = (
                    f"✅ **{escape_rich_inline(t_name)}** — Auto-approved."
                )
                await edit_rich(
                    bot, chat_id, msg_id, compact, reply_markup=None,
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
    from open_shrimp.handlers.utils import (
        _get_context,
        _get_context_name,
        chat_scope_from_message,
    )

    # Scope the policy lookup to the chat that owns this callback: each
    # per-context backend may render different keyboards / match different
    # bash patterns.
    callback_scope = (
        chat_scope_from_message(query.message) if query.message else None
    )
    p = _resolve_policy(None, scope=callback_scope)

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
                await edit_message_rich(
                    query.message,
                    expanded_text,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to expand Agent prompt")
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
            # An approval only exists because a turn is running, which needs a
            # bound context; the guard keeps the session key honest regardless.
            # Only the name keys the store, so the cheaper resolver is enough.
            ctx_name = await _get_context_name(scope, config, db)
            if ctx_name is not None:
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
                status = "\n\n✅ **Approved.** *All future edits auto-approved.*"
                await edit_message_rich(
                    query.message,
                    body_of(query.message) + status,
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
                policy=p, scope=callback_scope,
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

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            # The rule describes the approval itself, so it is built whatever
            # the scope is bound to; only remembering it needs a context, whose
            # name keys the session store and whose directory receives it.
            rule = ApprovalRule(tool_name="Bash", pattern=f"{prefix} *")
            resolved = await _get_context(scope, config, db)
            if resolved is not None:
                ctx_name, ctx_config = resolved
                _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)

                try:
                    persisted = await p.persist_session_rule(
                        rule, directory=ctx_config.directory, scope=scope,
                    )
                except OSError:
                    logger.exception("Failed to persist rule via backend policy")
                    persisted = False

                logger.info(
                    "Saved persistent Bash(%s:*) rule for scope %s context %s (persisted=%s)",
                    prefix,
                    scope,
                    ctx_name,
                    persisted,
                )

        future.set_result(True)
        await query.answer(
            f"Approved. Rule saved: {prefix} * auto-approved."
        )

        if query.message:
            try:
                compact = (
                    f"✅ **Bash** — Approved. "
                    f"*Rule saved: `{prefix} *` auto-approved.*"
                )
                await edit_message_rich(
                    query.message,
                    compact,
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
                policy=p, scope=callback_scope,
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
        await query.answer(
            f"Approved. {directory}/ allowed for this session."
        )

        if query.message:
            try:
                status = (
                    f"\n\n✅ **Approved.** "
                    f"*All future tool calls in `{directory}` "
                    f"auto-approved this session.*"
                )
                await edit_message_rich(
                    query.message,
                    body_of(query.message) + status,
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
                scope=callback_scope,
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
            # The rule describes the approval itself, so it is built whatever
            # the scope is bound to; only remembering it needs a context, whose
            # name keys the session store.
            rule = ApprovalRule(tool_name=accepted_tool_name, pattern=None)
            ctx_name = await _get_context_name(scope, config, db)
            if ctx_name is not None:
                _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)
                logger.info(
                    "Accept-all-%s enabled for scope %s context %s",
                    accepted_tool_name,
                    scope,
                    ctx_name,
                )

        future.set_result(True)
        await query.answer(
            f"Approved. All future {accepted_tool_name} calls will be auto-approved."
        )

        if query.message:
            try:
                status = (
                    f"\n\n✅ **Approved.** *All future "
                    f"{escape_rich_inline(accepted_tool_name)} "
                    f"calls auto-approved.*"
                )
                await edit_message_rich(
                    query.message,
                    body_of(query.message) + status,
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
                policy=p, scope=callback_scope,
            )
        return True

    # host_bash (sudo mode) and config-write approve/deny.  Both senders own
    # their card's text and edit it themselves once the future resolves, so
    # this only has to answer the tap.
    _OWN_CARD_PREFIXES = (
        _HOST_BASH_APPROVE_PREFIX, _HOST_BASH_DENY_PREFIX,
        _CONFIG_WRITE_APPROVE_PREFIX, _CONFIG_WRITE_DENY_PREFIX,
    )
    if data.startswith(_OWN_CARD_PREFIXES):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval is no longer live.")
            return True
        approved = data.startswith(
            (_HOST_BASH_APPROVE_PREFIX, _CONFIG_WRITE_APPROVE_PREFIX),
        )
        future.set_result(approved)
        await query.answer("Approved." if approved else "Denied.")
        return True

    # The three-way ask_context outer approval card owns its own button
    # semantics in cross_context, so delegate rather than duplicate them here.
    if await handle_handoff_callback(query, data):
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
        # status).  For Bash-like tools, collapse to a compact one-liner:
        # the card that lands when the command returns carries the command
        # and its output already.
        if query.message:
            try:
                icon = '✅' if approved else '❌'
                if tool_name and p.is_bash_like(tool_name):
                    body = (
                        f"{icon} **{escape_rich_inline(tool_name)}** "
                        f"— {action}."
                    )
                else:
                    body = body_of(query.message) + f"\n\n{icon} *{action}.*"
                await edit_message_rich(
                    query.message,
                    body,
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")
        return True

    return False
