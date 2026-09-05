"""Stream bridge between backend events and Telegram rich messages.

Consumes streaming events from agent.py, buffers text, and animates a rich
draft at intervals.  Handles message length limits, tool call notifications,
and final message delivery.

The buffer is a list of blocks rather than one string because two kinds of
text share it: GFM the agent produced, which has to be escaped before it
reaches Telegram, and rich markup this module built, which must not be.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from open_shrimp.backend.policy import BackendPolicy
    from open_shrimp.backend.protocol import BackendCopy, ChecklistReader

from open_shrimp.backend.types import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TERMINAL_TASK_STATUSES,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest

from open_shrimp.agent import AgentEvent
from open_shrimp.db import ChatScope
from open_shrimp.markdown import (
    RICH_MAX_LENGTH,
    escape_rich,
    gfm_to_rich_text,
    rich_code_block,
    rich_details,
    split_message,
)
from open_shrimp.mini_app import make_web_app_button
from open_shrimp.rich_message import (
    DRAFT_TTL_SECONDS,
    draft_budget_spent,
    edit_rich,
    send_rich,
    send_rich_draft,
)
from open_shrimp.tool_cards import (
    BASH_INTERRUPTED_NOTE,
    BASH_NO_OUTPUT_NOTE,
    bash_card,
    task_report_card,
    tool_summary_row,
    truncate_output,
)

logger = logging.getLogger(__name__)


def _is_thread_not_found(exc: BaseException) -> bool:
    """Whether ``exc`` is Telegram's 'message thread not found' error.

    Streaming lets this propagate instead of retrying so a turn whose
    forum topic was deleted stops sending instead of spraying messages
    into the parent chat.
    """
    return (
        isinstance(exc, BadRequest)
        and "message thread not found" in str(exc).lower()
    )

DRAFT_INTERVAL_SECONDS = 0.5

#: Re-send an unchanged draft once it has gone this long without an update.
#: A client deletes a live draft ``DRAFT_TTL_SECONDS`` after the last one it
#: received, so a turn that goes quiet for longer loses the draft it is still
#: writing into: a Bash call that runs a minute, an approval waiting on a tap,
#: a gap between a tool result and the next token.  Half the TTL clears the
#: 25-second worst case the rate limiter can defer a refresh by.
DRAFT_KEEPALIVE_SECONDS = DRAFT_TTL_SECONDS / 2


@dataclass
class StreamResult:
    """Result from stream_response() with session and usage info."""

    session_id: str | None = None
    model_usage: dict[str, Any] | None = None
    #: Per-turn token usage from the last AssistantMessage (API ``usage``
    #: object).  Contains ``input_tokens``, ``output_tokens``,
    #: ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
    turn_usage: dict[str, Any] | None = None
    num_turns: int = 0
    duration_ms: int = 0
    #: Finalized Telegram message ids for this turn, in send order.
    #: A snapshot of the slice ``state.sent_message_ids`` grew by during
    #: the turn — consumers (Backend.on_turn_end) read the last id to
    #: attach per-turn affordances (e.g. the prompt-suggestion button).
    sent_message_ids: list[int] = field(default_factory=list)
    #: True iff this turn emitted an ``AssistantMessage`` with an
    #: ``error`` field set (auth / billing / invalid_request /
    #: server_error).  Per-turn affordances may skip work when the turn
    #: didn't produce a useful assistant reply.
    last_turn_had_error: bool = False
    #: Count of ``AssistantMessage`` events observed in this turn.
    #: ``None`` if no assistant message arrived (e.g. cancelled before
    #: first reply); otherwise the in-stream count.
    assistant_turn_count: int | None = None


@dataclass
class _Block:
    """One run of the outgoing message.

    ``rich`` markup was built here and goes out untouched; everything else is
    GFM from the agent and is escaped on the way out, so a ``<details>`` the
    agent typed reads as text while the one this module built collapses.
    """

    rich: bool
    text: str
    #: ``render()``'s answer, and the ``text`` it was computed from.  Only the
    #: trailing block grows during a turn, so caching here turns the twice-a-
    #: second flush from one markdown parse per block into one per turn.
    _rendered: str = ""
    _rendered_from: str | None = None

    def render(self) -> str:
        if self.rich:
            return self.text
        if self._rendered_from != self.text:
            self._rendered = gfm_to_rich_text(self.text)
            self._rendered_from = self.text
        return self._rendered


@dataclass
class _DraftState:
    """Internal state for message drafting."""

    chat_id: int
    thread_id: int | None = None
    # Blocks accumulated so far, in send order.
    buffer: list[_Block] = field(default_factory=list)
    # tool_use_id -> index into buffer, for a tool card still waiting on its
    # result.  The row itself is the block at that index.
    tool_cards: dict[str, int] = field(default_factory=dict)
    # Message IDs of finalized messages (for reference)
    sent_message_ids: list[int] = field(default_factory=list)
    # Draft ID for sendMessageDraft (non-zero integer, stable per draft)
    draft_id: int = field(default_factory=lambda: random.randint(1, 2**31 - 1))
    # Whether the draft needs to be flushed
    dirty: bool = False
    # Monotonic time of the last draft Telegram accepted, 0.0 when none has
    # landed yet.  Drives the keepalive re-send that keeps the draft from
    # expiring during a quiet stretch.
    last_draft_sent: float = 0.0
    # Whether drafts are disabled (e.g. unsupported chat type)
    drafts_disabled: bool = False
    # Message ID of the current "live edit" message (fallback when drafts
    # are disabled — we send a real message and keep editing it).
    live_edit_message_id: int | None = None
    # Snapshot of the rendered body last sent via editMessageText, so we
    # can skip no-op edits.
    live_edit_last_text: str = ""
    # Whether the last assistant turn has completed (AssistantMessage seen).
    # Used to start a fresh block for text from the next turn.
    turn_complete: bool = False
    # Reasoning text for the turn in flight.  It rides in the draft's
    # <tg-thinking> block and is dropped from the message that replaces the
    # draft, so it never reaches the transcript.
    thinking: str = ""
    # Session ID captured as early as possible (from SystemMessage init or
    # ResultMessage) so it survives task cancellation.
    session_id: str | None = None
    # Map tool_use_id -> (tool_name, tool_input) for correlating tool results
    # to invocations and displaying context (e.g. Bash command + output).
    tool_use_map: dict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    # tool_use_ids of background agent tasks.  Messages with a matching
    # parent_tool_use_id are suppressed from the Telegram chat (the user
    # can watch progress via the terminal viewer instead).
    bg_task_tool_use_ids: set[str] = field(default_factory=set)
    # tool_use_id -> (icon, label, started_at) for a Bash call still awaiting
    # its result.  The render arguments ride along so the row can be rewritten
    # without re-deriving whether it came from Bash or host_bash, and the
    # monotonic start stamps the elapsed time onto the collapsed summary.  The
    # row's place in the buffer is in ``tool_cards``, which a host escape has
    # no entry in: it records its start without adding a row, because its
    # approval prompt is what shows the command while it waits.  Dropped once
    # the result arrives.
    pending_bash_cards: dict[str, tuple[str, str, float]] = field(
        default_factory=dict
    )
    # Fields for web_app button fallback in group chats.
    user_id: int = 0
    is_private_chat: bool = True
    bot_token: str = ""

    @property
    def _thread_kwargs(self) -> dict[str, Any]:
        """Build message_thread_id kwargs for Telegram send methods."""
        if self.thread_id is not None:
            return {"message_thread_id": self.thread_id}
        return {}

    @property
    def has_content(self) -> bool:
        return any(block.text.strip() for block in self.buffer)

    def append_gfm(self, text: str) -> None:
        """Append agent text, extending the trailing GFM run when there is one.

        Deltas arrive mid-word, so they have to land in the same run or a
        sentence would gain a paragraph break every few characters.
        """
        if self.buffer and not self.buffer[-1].rich:
            self.buffer[-1].text += text
        else:
            self.buffer.append(_Block(rich=False, text=text))

    def append_rich(self, markup: str) -> int:
        """Append markup built here and return its index for later rewriting."""
        self.buffer.append(_Block(rich=True, text=markup))
        return len(self.buffer) - 1

    def clear_buffer(self) -> None:
        self.buffer.clear()
        # The indices they hold are gone with the blocks.
        self.tool_cards.clear()

    def begin_new_message(self) -> None:
        """Drop everything that belongs to the message just sent.

        One owner, because the turn-end path and the out-of-band-send path
        both need it and a field reset in only one of them is invisible.
        """
        self.clear_buffer()
        self.draft_id = random.randint(1, 2**31 - 1)
        self.dirty = False
        self.last_draft_sent = 0.0
        self.turn_complete = False
        self.thinking = ""
        self.live_edit_message_id = None
        self.live_edit_last_text = ""


def _build_full_text(state: _DraftState) -> str:
    """Render the buffer into one rich-message body."""
    parts = [block.render().strip() for block in state.buffer]
    return "\n\n".join(part for part in parts if part)


def _draft_is_stale(state: _DraftState) -> bool:
    """Whether the live draft is close enough to its TTL to need re-sending.

    Only for real drafts: the fallback path edits an ordinary message, which
    Telegram keeps until it is edited again.
    """
    if state.drafts_disabled or not state.last_draft_sent:
        return False
    if not state.has_content and not state.thinking:
        return False
    return time.monotonic() - state.last_draft_sent >= DRAFT_KEEPALIVE_SECONDS


async def _send_draft(bot: Bot, state: _DraftState) -> None:
    """Send or update a draft message via sendMessageDraft.

    When drafts are disabled (unsupported chat type), falls back to
    sending a real message and editing it in-place for a streaming effect.
    """
    if state.drafts_disabled:
        await _send_live_edit(bot, state)
        return

    if draft_budget_spent(state.chat_id):
        # Rendering below is the expensive part of a tick — a markdown parse
        # of every block that changed — and the send would only be refused.
        # The state stays dirty, so the next tick spends it on newer text.
        return

    full_text = _build_full_text(state)
    if state.thinking:
        # <tg-thinking> renders in a draft and nowhere else, which is exactly
        # the lifetime reasoning text should have.  It goes last because the
        # buffer above it is already written: reasoning that arrives between
        # two tool calls is newer than the answer's opening paragraph, and
        # putting it on top pushes a whole turn of rows down under it.
        thinking = (
            f"<tg-thinking>{escape_rich(state.thinking.strip())}</tg-thinking>"
        )
        full_text = f"{full_text}\n\n{thinking}" if full_text else thinking
    if not full_text.strip():
        return

    chunks = split_message(full_text, RICH_MAX_LENGTH)
    if not chunks:
        return

    # Use only the first chunk for the current draft
    # (overflow is handled at finalization)
    text = chunks[0]

    try:
        # False is the budget going while the text was rendered, not a
        # failure — same treatment as the gate above.
        if await send_rich_draft(
            bot, state.chat_id, state.draft_id, text,
            thread_id=state.thread_id,
        ):
            state.dirty = False
            state.last_draft_sent = time.monotonic()
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        error_msg = str(e).lower()
        if "draft_peer_invalid" in error_msg:
            # A rich draft's chat_id is Integer-only, so a group chat has to
            # fall back to editing a real message.
            logger.info("Drafts not supported for chat %s, disabling", state.chat_id)
            state.drafts_disabled = True
            # Immediately try the live-edit fallback so the user doesn't
            # wait until the next periodic flush.
            await _send_live_edit(bot, state)
        else:
            logger.exception("Failed to send draft message")


async def _send_live_edit(bot: Bot, state: _DraftState) -> None:
    """Fallback streaming: send a message and keep editing it in-place.

    Used when a rich draft is not supported (e.g. group chats).
    """
    full_text = _build_full_text(state)
    if not full_text.strip():
        return

    # Skip if nothing changed since last edit.  Clearing the flag matters:
    # reasoning deltas mark the state dirty but never reach a real message,
    # so a thinking phase would otherwise re-render the whole buffer twice a
    # second and find the same text every time.
    if full_text == state.live_edit_last_text:
        state.dirty = False
        return

    chunks = split_message(full_text, RICH_MAX_LENGTH)
    if not chunks:
        return

    # If the text overflows into multiple chunks, we need to finalize.
    if len(chunks) > 1:
        return

    text = chunks[0]

    if state.live_edit_message_id is None:
        # First flush — send a new message.
        try:
            msg = await send_rich(
                bot, state.chat_id, text,
                thread_id=state.thread_id,
                disable_notification=True,
            )
            state.live_edit_message_id = msg.message_id
            state.live_edit_last_text = full_text
            state.dirty = False
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to send live-edit message")
    else:
        # Update the existing message.
        try:
            await edit_rich(
                bot, state.chat_id, state.live_edit_message_id, text,
            )
            state.live_edit_last_text = full_text
            state.dirty = False
        except Exception as e:
            error_msg = str(e).lower()
            if "message is not modified" in error_msg:
                state.dirty = False
            else:
                logger.exception("Failed to edit live-edit message")


async def _finalize_message(
    bot: Bot, state: _DraftState, *, silent: bool = True,
) -> list[int]:
    """Finalize the draft by sending the full message.

    If a live-edit message exists, the first chunk is delivered by editing
    that message in-place (avoiding a duplicate), and any overflow chunks
    are sent as new messages.

    Args:
        silent: If True, send with ``disable_notification=True`` so the
            user's device doesn't buzz for intermediate messages.

    Returns list of sent message IDs.
    """
    full_text = _build_full_text(state)
    if not full_text.strip():
        return []

    chunks = split_message(full_text, RICH_MAX_LENGTH)
    if not chunks:
        return []

    message_ids: list[int] = []

    for i, chunk in enumerate(chunks):
        # Reuse the live-edit message for the first chunk.
        if i == 0 and state.live_edit_message_id is not None:
            try:
                await edit_rich(
                    bot, state.chat_id, state.live_edit_message_id, chunk,
                )
            except Exception:
                logger.exception("Failed to finalize live-edit message")
                # Fallback: the markup is what Telegram rejected, so retry
                # as an unformatted message rather than lose the text.
                try:
                    await bot.edit_message_text(
                        chat_id=state.chat_id,
                        message_id=state.live_edit_message_id,
                        text=chunk,
                    )
                except Exception:
                    logger.exception("Failed to finalize live-edit plaintext fallback")
            message_ids.append(state.live_edit_message_id)
            state.live_edit_message_id = None
            state.live_edit_last_text = ""
            continue

        try:
            msg = await send_rich(
                bot, state.chat_id, chunk,
                thread_id=state.thread_id,
                disable_notification=silent,
            )
            message_ids.append(msg.message_id)
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to send finalized message chunk")
            try:
                msg = await bot.send_message(
                    chat_id=state.chat_id,
                    text=chunk,
                    **state._thread_kwargs,
                    **({"disable_notification": True} if silent else {}),
                )
                message_ids.append(msg.message_id)
            except Exception as e2:
                if _is_thread_not_found(e2):
                    raise
                logger.exception("Failed to send plaintext fallback")

    return message_ids


def _resolve_policy(
    policy: "BackendPolicy | None",
    scope: "ChatScope | None" = None,
) -> "BackendPolicy":
    if policy is not None:
        return policy
    from open_shrimp.client_manager import resolve_backend

    return resolve_backend(scope=scope).policy


def extract_tool_summary(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None = None,
    policy: "BackendPolicy | None" = None,
) -> str:
    """Extract a brief summary from tool input for notifications."""
    return _resolve_policy(policy).summarize(tool_name, tool_input, cwd)


def _extract_bash_output_text(
    content: str | list[dict[str, Any]] | None,
) -> str:
    """Extract plain text from Bash tool result content."""
    if content is None:
        return ""
    if isinstance(content, list):
        parts = [
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return content


def _place_card(state: _DraftState, tool_use_id: str, card: str) -> None:
    """Rewrite the row this invocation left in the draft, or start a new one.

    The row is gone whenever an approval prompt finalized the draft between the
    command and its result; the collapsed card then opens the next message
    rather than chasing a message the buffer no longer owns.
    """
    index = state.tool_cards.pop(tool_use_id, None)
    if index is None or index >= len(state.buffer):
        state.append_rich(card)
    else:
        state.buffer[index].text = card
    state.dirty = True


def _open_bash_card(
    state: _DraftState,
    tool_use_id: str,
    tool_input: dict[str, Any],
    icon: str = "💻",
    label: str = "Bash",
    post: bool = True,
) -> None:
    """Put the command in the draft when the agent issues it, before it runs.

    The draft flushes twice a second, so a command that takes a minute is on
    screen at second zero; ``_close_bash_card`` rewrites the row in place when
    the result arrives.  Nothing is sent here — a Bash call costs a row in the
    turn's message, not a message.

    With *post* false only the start stamp is recorded, so the card still
    reports how long the command took.  That is the host-escape case: its
    approval prompt is already showing the command and the command cannot
    start until the user taps, so a second copy above it says nothing.
    """
    state.pending_bash_cards[tool_use_id] = (icon, label, time.monotonic())
    if not post:
        return
    state.tool_cards[tool_use_id] = state.append_rich(
        bash_card(tool_input, icon, label, open=True),
    )
    state.dirty = True


def _close_bash_card(
    state: _DraftState,
    tool_use_id: str,
    tool_input: dict[str, Any],
    content: str | list[dict[str, Any]] | None,
    icon: str = "💻",
    label: str = "Bash",
    is_error: bool = False,
) -> None:
    """Collapse the row onto its result.

    The output rides inside the card, capped by ``truncate_output``: one
    message carries one keyboard, and the turn's message is shared by every
    row, so there is nowhere to hang a "reveal the rest" button.

    Background tasks get their "View output" button from the
    ``TaskStartedMessage`` handler instead, so their card stays output-free.
    """
    pending = state.pending_bash_cards.pop(tool_use_id, None)
    elapsed = time.monotonic() - pending[2] if pending else None

    output: str | None = None
    note: str | None = None
    if not tool_input.get("run_in_background"):
        output_text = _extract_bash_output_text(content).strip()
        if output_text:
            output, _ = truncate_output(output_text)
        else:
            note = BASH_NO_OUTPUT_NOTE

    _place_card(state, tool_use_id, bash_card(
        tool_input, icon, label,
        output=output,
        note=note,
        elapsed=elapsed,
        is_error=is_error,
        open=False,
    ))


def _drop_open_bash_cards(state: _DraftState) -> None:
    """Blank rows for commands still running as the draft is finalized.

    An open card in a sent message can never be collapsed — the buffer that
    owns the row is about to be cleared — so it would sit there expanded while
    the result posts a second copy of the same command below it.  Blanking
    rather than removing keeps the indices of the rows around it valid;
    ``_build_full_text`` drops empty blocks.
    """
    for tool_use_id in state.pending_bash_cards:
        index = state.tool_cards.pop(tool_use_id, None)
        if index is not None and index < len(state.buffer):
            state.buffer[index].text = ""


def _clear_pending_bash_cards(state: _DraftState) -> None:
    """Mark cards that never got a result when the stream ends.

    Every tool_use is followed by its result, so a leftover here means the
    turn was interrupted or errored out before the tool reported back.  Runs
    before the turn's last finalize, so the mark reaches the message the row
    is already sitting in rather than trailing it.
    """
    for tool_use_id, (icon, label, started_at) in list(
        state.pending_bash_cards.items()
    ):
        tool_info = state.tool_use_map.get(tool_use_id)
        if tool_info is None:
            continue
        _place_card(state, tool_use_id, bash_card(
            tool_info[1], icon, label,
            note=BASH_INTERRUPTED_NOTE,
            elapsed=time.monotonic() - started_at,
            open=False,
        ))
    state.pending_bash_cards.clear()


async def finalize_and_reset(
    bot: Bot, state: _DraftState, *, silent: bool = True,
) -> None:
    """Finalize the current draft and reset state for a new message.

    Call this before sending an out-of-band message (e.g. tool approval
    keyboard) to ensure correct message ordering in Telegram.

    Args:
        silent: If True, send the finalized message silently (no notification).
    """
    _drop_open_bash_cards(state)
    if state.has_content:
        msg_ids = await _finalize_message(bot, state, silent=silent)
        state.sent_message_ids.extend(msg_ids)
    state.begin_new_message()


#: Neutral fallback messages for the vendor-agnostic error codes a backend
#: may emit on ``AssistantMessage.error``.  Per-backend overrides come
#: through ``BackendCopy.assistant_error_messages``; missing keys land here,
#: missing here land in the generic ``⚠️ Error: <code>`` fallback.
_DEFAULT_ASSISTANT_ERROR_MESSAGES: dict[str, str] = {
    "rate_limit": (
        "⚠️ **Rate limited.** Too many requests — please wait a moment "
        "and try again."
    ),
    "unknown": (
        "⚠️ **Unknown error.** An unexpected error occurred."
    ),
}


async def _handle_assistant_error(
    bot: Bot, state: _DraftState, error: str,
    error_detail: str | None = None,
    copy: "BackendCopy | None" = None,
) -> None:
    """Send a user-friendly error message for AssistantMessage errors."""
    if error_detail:
        logger.warning(
            "AssistantMessage error for chat %d: %s (%s)",
            state.chat_id, error, error_detail,
        )
    else:
        logger.warning(
            "AssistantMessage error for chat %d: %s",
            state.chat_id, error,
        )

    table = copy.assistant_error_messages if copy else {}
    msg_text = (
        table.get(error)
        or _DEFAULT_ASSISTANT_ERROR_MESSAGES.get(error)
        or f"⚠️ **Error:** {error}"
    )
    # Append the detail from the backend (e.g. "Prompt is too long") so
    # the user knows *why* the request was rejected.
    if error_detail:
        msg_text += f"\n\n> {error_detail}"

    await finalize_and_reset(bot, state)
    try:
        await send_rich(
            bot, state.chat_id, gfm_to_rich_text(msg_text),
            thread_id=state.thread_id,
        )
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        logger.exception("Failed to send error message for %s", error)
        try:
            await bot.send_message(
                chat_id=state.chat_id,
                text=msg_text,
                **state._thread_kwargs,
            )
        except Exception as e2:
            if _is_thread_not_found(e2):
                raise
            logger.exception("Failed to send plaintext error fallback")


async def stream_response(
    bot: Bot,
    chat_id: int,
    events: AsyncIterator[AgentEvent],
    draft_state: _DraftState | None = None,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
    on_todo_update: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    terminal_base_url: str | None = None,
    scope: ChatScope | None = None,
    policy: "BackendPolicy | None" = None,
    copy: "BackendCopy | None" = None,
    checklist_reader: "ChecklistReader | None" = None,
) -> StreamResult:
    """Stream backend events to Telegram as draft messages.

    Consumes events from the agent, buffers text, sends drafts at intervals,
    and finalizes when the result is received.

    Args:
        bot: Telegram Bot instance.
        chat_id: Telegram chat ID to send messages to.
        events: Async iterator of AgentEvent from the backend client
            (client_manager.query_and_stream / receive_events).
        draft_state: Optional pre-created draft state. If provided, the
            same state can be shared with tool approval callbacks so they
            can finalize the draft before sending approval keyboards,
            ensuring correct message ordering.
        allowed_tools: List of allowed tool patterns from config.
            Used to tag inline tool notifications as "(auto)".
        cwd: Working directory for the current context. When set, file
            paths under this directory are shown as relative paths.
        checklist_reader: Async ``session_id -> checklist`` reader from
            ``Backend.checklist_reader``. When set, checklist-tool results
            (``policy.is_checklist_tool``) trigger a re-read that fires
            ``on_todo_update`` with the current list.

    Returns:
        StreamResult with session_id, usage, cost, and timing info.
    """
    state = draft_state or _DraftState(chat_id=chat_id)
    auto_set = set(allowed_tools or [])
    result = StreamResult()
    # Snapshot the sent_message_ids length so the on_turn_end hook gets
    # only the ids this turn appended (the same ``_DraftState`` is reused
    # across turns when the caller passes one in).
    sent_message_start = len(state.sent_message_ids)
    draft_task: asyncio.Task[None] | None = None
    p = _resolve_policy(policy, scope=scope)
    # Whether a checklist tool ran this turn.  Lets the turn-end read
    # distinguish "the agent emptied the list" (push the clear) from "the
    # turn never touched the checklist" (skip the no-op update).
    checklist_touched = False
    # The last checklist pushed to on_todo_update this turn, for change
    # detection: identical consecutive reads (e.g. the turn-end read after
    # a tool-time read) skip the redundant pinned-message edit.
    last_pushed: list[dict[str, Any]] | None = None

    async def fire_todo_update(todos: list[dict[str, Any]]) -> None:
        nonlocal last_pushed
        if on_todo_update is None or todos == last_pushed:
            return
        last_pushed = todos
        try:
            await on_todo_update(todos)
        except Exception:
            logger.exception(
                "Failed to update todos for chat %d", state.chat_id,
            )

    async def refresh_checklist() -> None:
        """Re-read the backend's checklist store and push the current list."""
        if on_todo_update is None or checklist_reader is None:
            return
        if not state.session_id:
            return
        try:
            todos = await checklist_reader(state.session_id)
        except Exception:
            logger.exception(
                "Checklist read failed for chat %d", state.chat_id,
            )
            return
        if not todos and not checklist_touched:
            return
        await fire_todo_update(todos)

    async def periodic_flush() -> None:
        """Flush dirty drafts, and refresh a quiet one before it expires."""
        while True:
            await asyncio.sleep(DRAFT_INTERVAL_SECONDS)
            if state.dirty or _draft_is_stale(state):
                await _send_draft(bot, state)

    try:
        draft_task = asyncio.create_task(periodic_flush())

        async for event in events:
            # Capture session_id from any event that carries one, as early
            # as possible — it's needed mid-turn (checklist store reads)
            # and must survive a cancel before the ResultMessage arrives.
            sid = getattr(event, "session_id", None)
            if sid:
                state.session_id = sid
                result.session_id = sid

            # Suppress sub-agent messages from background tasks.
            _parent = getattr(event, "parent_tool_use_id", None)
            if _parent and _parent in state.bg_task_tool_use_ids:
                continue

            if isinstance(event, AssistantMessage):
                result.assistant_turn_count = (
                    (result.assistant_turn_count or 0) + 1
                )

                # Capture per-turn token usage.
                turn_usage = event.usage
                if turn_usage:
                    result.turn_usage = turn_usage

                # Check for backend-level errors (auth failures, billing,
                # rate limits, etc.) and surface them to the user.
                if event.error:
                    result.last_turn_had_error = True
                    # Extract error detail from content blocks (the backend
                    # puts the human-readable reason in a TextBlock, e.g.
                    # "Prompt is too long").
                    error_detail = None
                    for block in event.content:
                        if isinstance(block, TextBlock) and block.text:
                            error_detail = block.text
                            break
                    await _handle_assistant_error(
                        bot, state, event.error, error_detail,
                        copy=copy,
                    )

                # Mark this turn's text as complete. When the next
                # turn's StreamEvent deltas arrive, they start their own
                # block to prevent text concatenation.
                if state.has_content:
                    state.turn_complete = True

                for block in event.content:
                    if isinstance(block, TextBlock):
                        # When include_partial_messages is enabled,
                        # text arrives via StreamEvent deltas. The
                        # AssistantMessage still arrives with the
                        # complete text, so we skip it here to avoid
                        # double-counting.
                        pass

                    elif isinstance(block, ToolUseBlock):
                        # Record the mapping so we can correlate tool
                        # results back to the tool that produced them.
                        state.tool_use_map[block.id] = (
                            block.name,
                            block.input,
                        )

                        # Acting ends a reasoning block.  ThinkingDeltaEvent
                        # carries no block boundary, so without a break here
                        # the next block's first delta runs into this one's
                        # last word: "…independently.I'll gather the manifest".
                        if state.thinking:
                            state.thinking = state.thinking.rstrip() + "\n\n"

                        # Add tool invocation as an inline notification,
                        # but suppress tools whose output is shown directly.
                        # A bash-like owns a card of its own a few lines
                        # down, which says everything the row would.
                        if not (
                            p.suppress_notification(block.name)
                            or p.is_bash_like(block.name)
                        ):
                            add_tool_notification(
                                state,
                                tool_use_id=block.id,
                                tool_name=block.name,
                                tool_input=block.input,
                                auto=block.name in auto_set,
                                cwd=cwd,
                                policy=p,
                            )

                        # Bash-likes get a card instead of a row, opened now so
                        # a long-running command is visible while it runs and
                        # collapsed in place when the result arrives.  A host
                        # escape waits on an approval card that already shows
                        # the command, so its card waits for the result.
                        if p.is_host_bash(block.name):
                            icon, label = p.host_bash_render()
                            _open_bash_card(
                                state, block.id, block.input,
                                icon=icon,
                                label=label,
                                post=False,
                            )
                        elif p.is_bash_like(block.name):
                            _open_bash_card(state, block.id, block.input)

                        # Checklist update: when the tool input carries the
                        # full list, push it to the pinned message directly.
                        # Incremental checklist tools are handled at their
                        # ToolResultBlock instead (post-execution, so the
                        # store read observes the mutation).
                        if p.is_checklist_tool(block.name):
                            snapshot = p.checklist_snapshot(
                                block.name, block.input,
                            )
                            if snapshot is not None:
                                await fire_todo_update(snapshot)

            elif isinstance(event, UserMessage):
                # UserMessage carries tool results (ToolResultBlock).  Every
                # tool folds its result into the row its invocation left in
                # the draft; a bash-like's row is a card carrying the command,
                # so it collapses onto the output instead of growing one.
                checklist_result = False
                if isinstance(event.content, list):
                    for block in event.content:
                        if isinstance(block, ToolResultBlock):
                            tool_info = state.tool_use_map.get(
                                block.tool_use_id,
                            )
                            if not tool_info:
                                continue
                            name = tool_info[0]
                            if p.is_host_bash(name):
                                icon, label = p.host_bash_render()
                                _close_bash_card(
                                    state, block.tool_use_id,
                                    tool_info[1], block.content,
                                    icon=icon,
                                    label=label,
                                    is_error=block.is_error,
                                )
                            elif p.is_bash_like(name):
                                _close_bash_card(
                                    state, block.tool_use_id,
                                    tool_info[1], block.content,
                                    is_error=block.is_error,
                                )
                            else:
                                fold_tool_result(
                                    state, block.tool_use_id, block.content,
                                    is_error=block.is_error,
                                    suppress_body=p.suppress_result_body(name),
                                )
                            if p.is_checklist_tool(name):
                                checklist_result = True
                # A checklist tool finished executing: re-read the store
                # and refresh the pinned message.  Coalesced per message
                # so several checklist calls cost one read + one edit.
                if checklist_result and checklist_reader is not None:
                    checklist_touched = True
                    await refresh_checklist()

            elif isinstance(event, TextDeltaEvent):
                text = event.text
                if text:
                    # The answer has started, so the reasoning that led to it
                    # has no more claim on the draft.
                    state.thinking = ""
                    if state.turn_complete:
                        # A new turn starts its own block, so its first delta
                        # can't run into the previous turn's last word.
                        state.buffer.append(_Block(rich=False, text=""))
                        state.turn_complete = False
                    state.append_gfm(text)
                    state.dirty = True

                    # Rendering the buffer parses the whole answer, so the
                    # cheap character count gates it.  Escaping only grows
                    # text, so a buffer under the ceiling in raw form can
                    # still overflow — the final split catches that.
                    raw_length = sum(len(b.text) for b in state.buffer)
                    if raw_length > RICH_MAX_LENGTH:
                        await _finalize_current(bot, state)

            elif isinstance(event, ThinkingDeltaEvent):
                if event.text:
                    state.thinking += event.text
                    state.dirty = True

            elif isinstance(event, ResultMessage):
                # Turn-end checklist read: subagent tool calls never appear
                # in the main stream but write to the same checklist store,
                # so a final read catches their changes.
                await refresh_checklist()
                result.model_usage = event.model_usage
                result.num_turns = event.num_turns
                result.duration_ms = event.duration_ms
                if event.errors:
                    logger.warning(
                        "ResultMessage errors for chat %d: %s",
                        state.chat_id,
                        event.errors,
                    )

            elif isinstance(event, SystemMessage):
                if isinstance(event, TaskStartedMessage):
                    logger.info(
                        "Background task started %s (%s) for chat %d: %s",
                        event.task_id,
                        event.task_type,
                        state.chat_id,
                        event.description,
                    )
                    # Track the task.
                    if scope is not None:
                        from open_shrimp.handlers.state import (
                            TrackedTask,
                            _active_bg_tasks,
                        )

                        scope_tasks = _active_bg_tasks.setdefault(scope, {})
                        scope_tasks[event.task_id] = TrackedTask(
                            task_id=event.task_id,
                            description=event.description,
                            task_type=event.task_type,
                            started_at=time.monotonic(),
                            tool_use_id=event.tool_use_id,
                            session_id=event.session_id,
                        )
                    if event.tool_use_id and p.is_subagent_task(event.task_type):
                        state.bg_task_tool_use_ids.add(event.tool_use_id)
                    # Send Telegram notification.
                    await finalize_and_reset(bot, state)
                    try:
                        desc = event.description or "Background task"
                        task_type_param = (
                            f"&task_type={event.task_type}"
                            if event.task_type
                            else ""
                        )
                        view_output = make_web_app_button(
                            "📺 View output",
                            terminal_base_url,
                            f"/terminal/?type=task&id={event.task_id}"
                            f"{task_type_param}",
                            chat_id=state.chat_id,
                            user_id=state.user_id,
                            bot_token=state.bot_token,
                            is_private_chat=state.is_private_chat,
                        )
                        keyboard = (
                            InlineKeyboardMarkup([[view_output]])
                            if view_output
                            else None
                        )
                        await send_rich(
                            bot, state.chat_id,
                            f"⏳ {escape_rich(desc)}",
                            thread_id=state.thread_id,
                            reply_markup=keyboard,
                            disable_notification=True,
                        )
                    except Exception as e:
                        if _is_thread_not_found(e):
                            raise
                        logger.exception(
                            "Failed to send task started message"
                        )

                elif isinstance(event, TaskProgressMessage):
                    logger.debug(
                        "Background task progress %s for chat %d: "
                        "last_tool=%s",
                        event.task_id,
                        state.chat_id,
                        event.last_tool_name,
                    )
                    if scope is not None:
                        from open_shrimp.handlers.state import _active_bg_tasks

                        scope_tasks = _active_bg_tasks.get(scope)
                        if scope_tasks and event.task_id in scope_tasks:
                            scope_tasks[event.task_id].last_tool_name = (
                                event.last_tool_name
                            )

                elif isinstance(event, TaskNotificationMessage):
                    # The summary is the subagent's whole report, so the line
                    # says how much came back rather than pasting it into the
                    # log on every completion.
                    logger.info(
                        "Background task %s %s for chat %d: %d chars",
                        event.task_id,
                        event.status,
                        state.chat_id,
                        len(event.summary or ""),
                    )
                    # Take the tracker from wherever it is: a terminal
                    # task_updated for the same task can land first and park
                    # it in the finished set.  It holds the description and
                    # the start stamp, which the notification carries
                    # neither of.
                    tracked = None
                    if scope is not None:
                        from open_shrimp.handlers.state import (
                            take_finished_task,
                        )

                        tracked = take_finished_task(scope, event.task_id)
                    description = tracked.description if tracked else None
                    elapsed = (
                        time.monotonic() - tracked.started_at if tracked else None
                    )
                    await finalize_and_reset(bot, state)
                    try:
                        card = task_report_card(
                            description,
                            event.summary,
                            status=event.status,
                            elapsed=elapsed,
                        )
                        for chunk in split_message(card):
                            await send_rich(
                                bot, state.chat_id, chunk,
                                thread_id=state.thread_id,
                                disable_notification=True,
                            )
                    except Exception as e:
                        if _is_thread_not_found(e):
                            raise
                        logger.exception(
                            "Failed to send task notification message"
                        )

                elif isinstance(event, TaskUpdatedMessage):
                    # A task's terminal state can arrive only as a
                    # task_updated patch with no accompanying notification
                    # (e.g. killed via TaskStop), so /tasks stops listing it
                    # here.  A completion sends both, patch first by a few
                    # milliseconds, so the tracker moves to the finished set
                    # rather than being dropped — the notification's card is
                    # built from it.  Stays silent either way: the
                    # TaskNotificationMessage owns the 📋.
                    if (
                        event.status in TERMINAL_TASK_STATUSES
                        and scope is not None
                    ):
                        from open_shrimp.handlers.state import finish_task

                        if finish_task(scope, event.task_id) is not None:
                            logger.info(
                                "Cleared task %s from tracking on terminal "
                                "task_updated (%s) for chat %d",
                                event.task_id,
                                event.status,
                                state.chat_id,
                            )

            elif isinstance(event, RateLimitEvent):
                # backend.types.RateLimitEvent is flat (the SDK's nested
                # rate_limit_info is flattened in the claude_sdk adapter's
                # translate.SdkTranslator).
                if event.status == "rejected":
                    logger.warning(
                        "Rate limit hit (%s) for chat %d, resets at %s",
                        event.rate_limit_type,
                        state.chat_id,
                        event.resets_at,
                    )
                    await finalize_and_reset(bot, state)
                    try:
                        await send_rich(
                            bot, state.chat_id,
                            "⚠️ Rate limit reached. Waiting for reset.",
                            thread_id=state.thread_id,
                            disable_notification=True,
                        )
                    except Exception as e:
                        if _is_thread_not_found(e):
                            raise
                        logger.exception("Failed to send rate limit message")
                elif event.status == "allowed_warning":
                    pct = (
                        f" ({event.utilization:.0%})"
                        if event.utilization is not None
                        else ""
                    )
                    logger.info(
                        "Rate limit warning%s for chat %d",
                        pct,
                        state.chat_id,
                    )

    finally:
        if draft_task:
            draft_task.cancel()
            try:
                await draft_task
            except asyncio.CancelledError:
                pass

        # Commands with no result get their mark before the send, not after:
        # the row is in the buffer about to go out.
        if state.pending_bash_cards:
            _clear_pending_bash_cards(state)

        # Final send of any remaining text — notify since the task is done.
        if state.has_content:
            msg_ids = await _finalize_message(bot, state, silent=False)
            state.sent_message_ids.extend(msg_ids)

        # Snapshot the message ids this turn produced for the per-turn
        # backend hook (Backend.on_turn_end).  The handler reads the
        # last id to attach the prompt-suggestion button; the slice
        # bound is recorded at turn start so we ignore ids from prior
        # turns sharing the same draft state.
        result.sent_message_ids = state.sent_message_ids[sent_message_start:]

        # Reset for the next stream_response() iteration.
        state.begin_new_message()

    return result


async def _finalize_current(bot: Bot, state: _DraftState) -> None:
    """Send everything but the tail of an answer that outgrew one message.

    The tail seeds the next message.  It goes back as rendered markup rather
    than GFM: ``split_message`` already chose a boundary the markup survives,
    and re-parsing it would escape the tags it just balanced.
    """
    chunks = split_message(_build_full_text(state), RICH_MAX_LENGTH)
    if len(chunks) <= 1:
        return

    for chunk in chunks[:-1]:
        try:
            msg = await send_rich(
                bot, state.chat_id, chunk,
                thread_id=state.thread_id,
                disable_notification=True,
            )
            state.sent_message_ids.append(msg.message_id)
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to finalize message")
            try:
                msg = await bot.send_message(
                    chat_id=state.chat_id,
                    text=chunk,
                    disable_notification=True,
                    **state._thread_kwargs,
                )
                state.sent_message_ids.append(msg.message_id)
            except Exception as e2:
                if _is_thread_not_found(e2):
                    raise
                logger.exception("Failed to send plaintext fallback")

    state.begin_new_message()
    state.buffer.append(_Block(rich=True, text=chunks[-1]))
    state.dirty = True


def add_tool_notification(
    state: _DraftState,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    auto: bool,
    cwd: str | None = None,
    policy: "BackendPolicy | None" = None,
) -> None:
    """Add a tool call to the draft as its own row.

    The row becomes a collapsible card once the result lands
    (``fold_tool_result``); until then there is nothing to collapse, so it
    stays a plain line and a call whose result never arrives still reads.
    """
    summary = extract_tool_summary(tool_name, tool_input, cwd=cwd, policy=policy)
    row = tool_summary_row(tool_name, summary, auto)
    state.tool_cards[tool_use_id] = state.append_rich(row)
    state.dirty = True


def fold_tool_result(
    state: _DraftState,
    tool_use_id: str,
    content: str | list[dict[str, Any]] | None,
    *,
    is_error: bool = False,
    suppress_body: bool = False,
) -> None:
    """Fold a tool's output into the card its invocation left in the draft.

    Does nothing when the card has already been sent — the draft is finalized
    whenever an out-of-band message has to go out first, and a sent message is
    no longer this buffer's to rewrite.

    With *suppress_body* a successful call stays a row: a tool that hands back
    what the row already names — a file read hands back the file — has nothing
    to add by repeating it under a chevron.  A failure still folds, because
    then the output is the reason it failed rather than the thing asked for.
    """
    index = state.tool_cards.pop(tool_use_id, None)
    if index is None or index >= len(state.buffer):
        return
    row = state.buffer[index].text

    if suppress_body and not is_error:
        return

    output = _extract_bash_output_text(content).strip()
    if not output:
        return

    body, _ = truncate_output(output)
    if is_error:
        row = row.replace("🔧", "⚠️", 1)
    state.buffer[index].text = rich_details(row, rich_code_block(body))
    state.dirty = True
