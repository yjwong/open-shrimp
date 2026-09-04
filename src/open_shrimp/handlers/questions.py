"""AskUserQuestion handling via Telegram inline keyboards.

The Telegram card is one of two ways to answer.  The Android companion
renders the same question from the agent-status push and answers it by
option index over ``/api/agent/questions/{question_id}``; both paths
resolve the one :class:`_QuestionState` future, so the answer the agent
receives is identical whichever surface produced it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.config import Config
from open_shrimp.db import ChatScope
from open_shrimp.handlers.approval import _close_card
from open_shrimp.handlers.state import (
    _QuestionState,
    _pending_other_input,
    _question_states,
)
from open_shrimp.handlers.utils import _is_authorized
from open_shrimp.markdown import escape_rich, escape_rich_inline
from open_shrimp.rich_message import (
    body_of,
    edit_message_rich,
    edit_rich,
    send_rich,
)
from open_shrimp.stream import _DraftState, finalize_and_reset

logger = logging.getLogger(__name__)

# Called once a question's card is up and its state registered, with the id
# plus the pieces a second surface needs to render the same choice — never the
# raw tool input, so the AskUserQuestion schema stays this module's business.
# The agent-status push needs a Config and a database handle that this module
# has no reason to hold, so the caller supplies the two edges instead.
QuestionOpened = Callable[
    [str, str, list[dict[str, Any]], bool], Awaitable[None]
]
QuestionsClosed = Callable[[], Awaitable[None]]


def option_label(options: list[dict[str, Any]], index: int) -> str:
    """The label at *index*, or a positional stand-in when it has none.

    The one place the fallback is spelled, because the button, the toast and
    the answer must all name an option the same way.
    """
    return options[index].get("label", f"Option {index + 1}")


def format_answer(
    options: list[dict[str, Any]],
    indexes: Iterable[int],
    other_texts: Sequence[str],
) -> str:
    """Render chosen option positions and free text as the agent's answer.

    Out-of-range positions are dropped rather than raising: the phone
    answers by index into a list it was pushed, and a stale push must not
    take the bot down.
    """
    labels = [
        option_label(options, i)
        for i in sorted(set(indexes))
        if 0 <= i < len(options)
    ]
    labels.extend(other_texts)
    return ", ".join(labels) if labels else "None selected"


def _build_question_keyboard(state: _QuestionState) -> InlineKeyboardMarkup:
    """Build inline keyboard for a question's options."""
    qid = state.question_id
    buttons: list[list[InlineKeyboardButton]] = []

    for i in range(len(state.options)):
        label = option_label(state.options, i)
        if state.multi_select:
            prefix = "\u2713 " if i in state.selected else ""
            cb_data = f"q_toggle:{qid}:{i}"
        else:
            prefix = ""
            cb_data = f"q_opt:{qid}:{i}"
        buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=cb_data)])

    # Show any "Other" texts already entered (multi-select)
    for j, txt in enumerate(state.other_texts):
        display = txt[:30] + ("\u2026" if len(txt) > 30 else "")
        buttons.append([InlineKeyboardButton(f"\u2713 {display}", callback_data=f"q_noop:{qid}")])

    # "Other" button for custom text input
    buttons.append([InlineKeyboardButton("Other\u2026", callback_data=f"q_other:{qid}")])

    if state.multi_select:
        count = len(state.selected) + len(state.other_texts)
        done_label = f"Done ({count} selected)" if count else "Done"
        buttons.append([InlineKeyboardButton(done_label, callback_data=f"q_done:{qid}")])

    return InlineKeyboardMarkup(buttons)


def _format_question_text(question: dict[str, Any]) -> str:
    """Format a question with its header and option descriptions."""
    question_text = question.get("question", "")
    header = question.get("header", "")
    options = question.get("options", [])

    parts: list[str] = []
    if header:
        parts.append(f"\u2753 **{escape_rich_inline(header)}**")
    parts.append(escape_rich(question_text))

    for opt in options:
        label = opt.get("label", "")
        desc = opt.get("description", "")
        if desc:
            parts.append(
                f"- **{escape_rich_inline(label)}** \u2014 "
                f"{escape_rich_inline(desc)}"
            )

    return "\n".join(parts)


async def _send_question_keyboard(
    bot: Bot,
    scope: ChatScope,
    question: dict[str, Any],
    on_open: QuestionOpened | None = None,
) -> str:
    """Present a question via inline keyboard and wait for the user's answer.

    Returns the selected option label (or custom "Other" text).
    """
    options = question.get("options", [])
    multi_select = question.get("multiSelect", False)
    question_id = uuid.uuid4().hex[:8]

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    state = _QuestionState(
        question_id=question_id,
        scope=scope,
        options=options,
        multi_select=multi_select,
        future=future,
        bot=bot,
    )
    _question_states[question_id] = state

    keyboard = _build_question_keyboard(state)
    text = _format_question_text(question)

    msg = await send_rich(
        bot, scope.chat_id, text,
        thread_id=scope.thread_id,
        reply_markup=keyboard,
    )
    state.message_id = msg.message_id
    state.original_text_md = text

    # Announced only once the card is up and the id is resolvable, so a phone
    # answering the instant the push lands finds the state it names.
    if on_open is not None:
        await on_open(
            question_id,
            question.get("question") or question.get("header") or text,
            options,
            bool(multi_select),
        )

    try:
        return await future
    finally:
        _question_states.pop(question_id, None)


async def _handle_ask_user_questions(
    bot: Bot,
    scope: ChatScope,
    questions: list[dict[str, Any]],
    draft_state: _DraftState,
    on_question_open: QuestionOpened | None = None,
    on_questions_closed: QuestionsClosed | None = None,
) -> dict[str, str]:
    """Present AskUserQuestion questions via Telegram and collect answers.

    Called from the PreToolUse hook when AskUserQuestion is intercepted.
    Finalizes any in-progress draft before presenting questions.  Questions
    are asked one at a time, and the overlay is torn down once at the end
    rather than between them: closing per question drops a three-question
    batch back to "Running" and re-alerts twice on the way through.
    """
    await finalize_and_reset(bot, draft_state)

    answers: dict[str, str] = {}
    try:
        for q in questions:
            question_text = q.get("question", "")
            answers[question_text] = await _send_question_keyboard(
                bot, scope, q, on_open=on_question_open,
            )
    finally:
        if on_questions_closed is not None:
            await on_questions_closed()

    return answers


async def resolve_question_from_device(
    question_id: str,
    option_indexes: Iterable[int],
    other_texts: Sequence[str],
) -> str | None:
    """Answer a live question from the Android companion.

    Returns the answer handed to the agent, or ``None`` when the question is
    already gone — answered in Telegram first, or the turn ended under it.
    """
    state = _question_states.get(question_id)
    if state is None or state.future.done():
        return None

    answer = format_answer(state.options, option_indexes, other_texts)
    state.future.set_result(answer)

    # The user may have tapped "Other…" in Telegram and still be typing; that
    # pending input now belongs to a question nobody is waiting on, and left
    # in place it would swallow their next message to the agent.
    if _pending_other_input.get(state.scope) == question_id:
        _pending_other_input.pop(state.scope, None)
    state.waiting_for_other = False

    # Nothing else will strike the card: the inline-button path edits it from
    # its own CallbackQuery, and an answer over HTTP has none.
    if state.bot is not None and state.message_id is not None:
        await _close_card(
            state.bot,
            state.scope.chat_id,
            state.message_id,
            state.original_text_md + f"\n\n✅ **Answer:** {escape_rich(answer)}",
        )
    return answer


async def _complete_other_input(
    bot: Bot,
    state: _QuestionState,
    custom_text: str,
) -> None:
    """Complete the 'Other...' flow after the user has typed their answer.

    For single-select questions, resolves the future immediately.
    For multi-select, adds the text to other_texts and updates the keyboard
    so the user can continue selecting or press Done.
    """
    query = state.other_query
    state.other_query = None

    original_md = state.original_text_md

    if state.multi_select:
        # Add to other_texts and restore keyboard; user still needs to press Done
        state.other_texts.append(custom_text)
        keyboard = _build_question_keyboard(state)
        if query and query.message:
            try:
                await edit_rich(
                    original_md,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to restore question keyboard after Other")
    else:
        # Single-select: resolve with custom text
        state.future.set_result(custom_text)
        if query and query.message:
            try:
                await edit_rich(
                    original_md
                    + f"\n\n\u2705 **Answer:** {escape_rich(custom_text)}",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message after Other")


async def _handle_question_callback(
    query: Any, data: str, config: Config
) -> bool:
    """Handle question-related callback queries. Returns True if handled."""
    if not data.startswith("q_"):
        return False

    if not _is_authorized(query.from_user and query.from_user.id, config):
        # Silent: a toast is the bot speaking to a non-allowlisted user.
        await query.answer()
        return True

    # Parse callback data
    parts = data.split(":", 2)
    action = parts[0]  # q_opt, q_toggle, q_done, q_other, q_noop

    if action == "q_noop":
        await query.answer()
        return True

    if len(parts) < 2:
        await query.answer("Invalid callback data.")
        return True

    question_id = parts[1]
    state = _question_states.get(question_id)
    if not state or state.future.done():
        await query.answer("This question has expired.")
        return True

    if state.waiting_for_other:
        await query.answer("Please type your answer first.")
        return True

    if action == "q_opt":
        # Single-select: resolve immediately with the selected option label
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            label = option_label(state.options, option_idx)
            state.future.set_result(label)
            await query.answer(f"Selected: {label}")

            # Update message to show selection, remove keyboard
            if query.message:
                try:
                    await edit_message_rich(
                        query.message,
                        body_of(query.message)
                        + f"\n\n\u2705 **Selected:** {escape_rich(label)}",
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("Failed to update question message")
        return True

    if action == "q_toggle":
        # Multi-select: toggle option
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            if option_idx in state.selected:
                state.selected.discard(option_idx)
            else:
                state.selected.add(option_idx)

            # Update keyboard to reflect toggled state
            keyboard = _build_question_keyboard(state)
            await query.answer()
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    logger.exception("Failed to update question keyboard")
        return True

    if action == "q_done":
        # Multi-select: finalize with all selected options
        result = format_answer(state.options, state.selected, state.other_texts)
        state.future.set_result(result)
        await query.answer(f"Done: {result[:50]}")

        # Update message to show selections, remove keyboard
        if query.message:
            try:
                await edit_rich(
                    body_of(query.message)
                    + f"\n\n\u2705 **Selected:** {escape_rich(result)}",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message")
        return True

    if action == "q_other":
        # "Other..." -- mark that we're waiting for a typed answer.
        # We must NOT await here because python-telegram-bot processes
        # updates sequentially by default; blocking would deadlock the
        # message_handler that needs to deliver the typed text.
        await query.answer()

        state.waiting_for_other = True
        state.other_query = query
        _pending_other_input[state.scope] = question_id

        # Hide the keyboard and prompt the user to type their answer.
        if query.message:
            try:
                await edit_rich(
                    body_of(query.message)
                    + "\n\n\u270f\ufe0f *Type your answer below:*",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message for Other prompt")
        return True

    return False
