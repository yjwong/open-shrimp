"""Shared mutable state for all bot handler modules.

All module-level dictionaries, sets, and constants that are shared across
handler modules live here.  This makes cross-module coupling explicit and
avoids circular imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Forward references (to avoid importing heavy modules at module level)
# ---------------------------------------------------------------------------
# AgentSession is referenced by type only; the actual import happens at
# usage sites in the handler modules.
from open_udang.client_manager import AgentSession
from open_udang.db import ChatScope

# ---------------------------------------------------------------------------
# Per-scope running asyncio task (for cancellation)
# ---------------------------------------------------------------------------
_running_tasks: dict[ChatScope, asyncio.Task[Any]] = {}

# ---------------------------------------------------------------------------
# Per-scope live session reference for message injection.
# Set once get_or_create_session + initial query() completes inside _run(),
# cleared in the finally block.
# ---------------------------------------------------------------------------
_injectable_sessions: dict[ChatScope, AgentSession] = {}

# ---------------------------------------------------------------------------
# Per-scope queue for messages that arrive during the brief setup phase
# (before the session is ready for injection).  Drained immediately once
# the session becomes injectable.
# ---------------------------------------------------------------------------
from open_udang.agent import FileAttachment

_setup_queues: dict[ChatScope, list[tuple[str, list[FileAttachment]]]] = {}

# ---------------------------------------------------------------------------
# Attachment temp-file paths created by injected messages.  Cleaned up in
# _run()'s finally block after the agent has finished processing.
# ---------------------------------------------------------------------------
_injected_attachment_paths: dict[ChatScope, list[Path]] = {}

# ---------------------------------------------------------------------------
# Pending tool approval futures: callback_data -> asyncio.Future[bool]
# ---------------------------------------------------------------------------
_approval_futures: dict[str, asyncio.Future[bool]] = {}

# ---------------------------------------------------------------------------
# Pending Agent tool inputs for "Show prompt" expansion: tool_use_id -> tool_input
# ---------------------------------------------------------------------------
_pending_agent_inputs: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Tool name for each pending approval: tool_use_id -> tool_name.
# Used to collapse verbose approval messages (e.g. Bash) to a compact
# one-liner after the user approves/denies.
# ---------------------------------------------------------------------------
_approval_tool_names: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Extended metadata for pending approvals: tool_use_id -> dict with
# tool_name, tool_input, chat_id, and message_id.  Used to auto-resolve
# parallel pending approvals when an "accept all" action is taken.
# ---------------------------------------------------------------------------
_approval_metadata: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Sessions where the user has opted into "accept all edits" for mutating
# file-access tools (Edit, Write) within the context working directory.
# Keyed by (scope, context_name).  Cleared on /clear or context switch.
# ---------------------------------------------------------------------------
_edit_approved_sessions: set[tuple[ChatScope, str]] = set()

# ---------------------------------------------------------------------------
# Per-session auto-approval rules for non-path-scoped tools (e.g.
# WebFetch, WebSearch, Bash).  Each rule can optionally carry a pattern
# (e.g. "git *" for Bash) so approval can be scoped to command prefixes.
# Cleared on /clear or context switch.
# ---------------------------------------------------------------------------
from open_udang.hooks import ApprovalRule

_tool_approved_sessions: dict[tuple[ChatScope, str], list[ApprovalRule]] = {}

# ---------------------------------------------------------------------------
# Per-scope model override: scope -> model name.  Set via /model command.
# Cleared on /clear or context switch.  Takes precedence over context config.
# ---------------------------------------------------------------------------
_model_overrides: dict[ChatScope, str] = {}

# ---------------------------------------------------------------------------
# Per-scope active background tasks.  Populated by TaskStartedMessage,
# updated by TaskProgressMessage, removed by TaskNotificationMessage.
# Cleared on /clear.
# ---------------------------------------------------------------------------


@dataclass
class TrackedTask:
    """A background task being tracked."""

    task_id: str
    description: str
    task_type: str | None  # "local_bash", "local_agent", "remote_agent"
    started_at: float  # time.monotonic()
    tool_use_id: str | None = None
    session_id: str | None = None
    last_tool_name: str | None = None  # updated by TaskProgressMessage


_active_bg_tasks: dict[ChatScope, dict[str, TrackedTask]] = {}


def is_task_active(task_id: str) -> bool:
    """Check whether a background task is still active (any scope)."""
    for scope_tasks in _active_bg_tasks.values():
        if task_id in scope_tasks:
            return True
    return False


# ---------------------------------------------------------------------------
# Media group batching: media_group_id -> list of messages received so far.
# ---------------------------------------------------------------------------
_media_group_messages: dict[str, list[Any]] = {}
_media_group_tasks: dict[str, asyncio.Task[Any]] = {}

# How long to wait for additional media group messages (seconds).
_MEDIA_GROUP_WAIT: float = 0.5

# ---------------------------------------------------------------------------
# AskUserQuestion state
# ---------------------------------------------------------------------------


@dataclass
class _QuestionState:
    """State for an active AskUserQuestion inline keyboard."""

    question_id: str
    scope: ChatScope
    options: list[dict[str, Any]]
    multi_select: bool
    future: asyncio.Future[str]
    selected: set[int] = field(default_factory=set)
    other_texts: list[str] = field(default_factory=list)
    message_id: int | None = None
    waiting_for_other: bool = False
    """True when the user clicked "Other..." and we're waiting for their text input."""
    other_query: Any = None
    """The callback query that triggered the "Other..." flow, used to edit the message afterward."""
    original_text_md: str = ""
    """The original MarkdownV2 message text, saved so we can restore it after Other input."""


# Pending question states: question_id -> _QuestionState
_question_states: dict[str, _QuestionState] = {}

# Pending "Other" text input: scope -> question_id.
# When message_handler sees a text message for a scope with a pending "Other"
# input, it resolves the question instead of dispatching to the agent.
_pending_other_input: dict[ChatScope, str] = {}

# ---------------------------------------------------------------------------
# Resume command state
# ---------------------------------------------------------------------------

# Maximum number of sessions to show in /resume list.
_RESUME_LIST_LIMIT = 10

# Pending resume selections: callback_data -> session_id
_resume_selections: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default context window for all Claude models via the Agent SDK (no 1M beta header).
_DEFAULT_CONTEXT_LIMIT = 200_000

# Status emoji map for MCP server connection status.
_MCP_STATUS_EMOJI: dict[str, str] = {
    "connected": "\U0001f7e2",
    "pending": "\U0001f7e1",
    "failed": "\U0001f534",
    "needs-auth": "\U0001f7e0",
    "disabled": "\u26aa",
}
