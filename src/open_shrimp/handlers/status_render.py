"""Rich-message rendering for the two status surfaces.

Kept in its own module so the renderers are unit-testable without a database,
a bot or a sandbox: :func:`render_status_card` takes a :class:`ScopeStatus`
the handler gathered and returns text plus a keyboard.

Both surfaces describe the same context — the pinned card at the top of the
topic and the ``/status`` card in the flow — so they share the icon vocabulary
and the model derivation here rather than each spelling them out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.backend import default_model_label
from open_shrimp.config import Config, ContextConfig, effective_backend
from open_shrimp.handlers.state import (
    _DEFAULT_CONTEXT_LIMIT,
    SessionApprovals,
    TrackedTask,
)
from open_shrimp.markdown import escape_rich, escape_rich_inline
from open_shrimp.sandbox.status import SandboxSnapshot
from open_shrimp.tool_cards import format_elapsed, plural, summary_row

# One vocabulary for both cards: change an icon here and the pinned message
# and the /status card move together.
PIN_ICON = "\U0001f4cc"
DIRECTORY_ICON = "\U0001f4c1"
MODEL_ICON = "\U0001f916"
EFFORT_ICON = "\U0001f9e0"
BACKEND_ICON = "\U0001f50c"
SESSION_ICON = "\U0001f194"
RUNNING_ICON = "\U0001f7e2"
IDLE_ICON = "⚪"
BLOCKED_ICON = "⏸️"
SANDBOX_ICON = "\U0001f512"
FORWARD_ICON = "\U0001f500"
APPROVED_ICON = "☑️"

# Every button on the status card.  ``status:clear`` only swaps the keyboard
# for a confirmation; ``status:clear!`` is the one that resets the session.
STATUS_PREFIX = "status:"
STATUS_REFRESH = "status:refresh"
STATUS_CANCEL = "status:cancel"
STATUS_CLEAR = "status:clear"
STATUS_CLEAR_CONFIRM = "status:clear!"
STATUS_MODEL = "status:model"
STATUS_CONTEXT = "status:context"


def effective_model(ctx: ContextConfig, config: Config) -> str:
    """The model *ctx* will actually use, named the way both cards name it."""
    return ctx.model or default_model_label(effective_backend(ctx, config))


@dataclass(frozen=True)
class ScopeStatus:
    """Everything the ``/status`` card shows, gathered before rendering."""

    context_name: str
    ctx: ContextConfig
    config: Config
    session_id: str | None
    running: bool
    elapsed: float | None
    injectable: bool
    queued: int
    awaiting_approval: bool
    approvals: SessionApprovals
    sandbox: SandboxSnapshot | None
    tasks: list[TrackedTask]
    model_overridden: bool = False
    effort_overridden: bool = False


def status_keyboard(running: bool) -> InlineKeyboardMarkup:
    """The status card's actions, with Cancel only while a task is live."""
    rows: list[list[InlineKeyboardButton]] = []
    if running:
        rows.append([
            InlineKeyboardButton("⏹️ Cancel", callback_data=STATUS_CANCEL),
        ])
    rows.append([
        InlineKeyboardButton("\U0001f504 Refresh", callback_data=STATUS_REFRESH),
        InlineKeyboardButton("\U0001f9f9 Clear session", callback_data=STATUS_CLEAR),
    ])
    rows.append([
        InlineKeyboardButton("\U0001f916 Model", callback_data=STATUS_MODEL),
        InlineKeyboardButton("\U0001f4c1 Context", callback_data=STATUS_CONTEXT),
    ])
    return InlineKeyboardMarkup(rows)


def clear_confirm_keyboard() -> InlineKeyboardMarkup:
    """Ask before resetting a session; "Keep it" is a plain refresh."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, clear it", callback_data=STATUS_CLEAR_CONFIRM),
        InlineKeyboardButton("✖️ Keep it", callback_data=STATUS_REFRESH),
    ]])


def _activity_block(status: ScopeStatus) -> str:
    """What the scope is doing, plus a line if it is parked on a human.

    Idle, not-injectable and nothing-queued are the same news, so they
    collapse into one word rather than three rows of "No".
    """
    lines: list[str] = []
    if status.running:
        parts: list[str] = []
        if status.elapsed is not None:
            parts.append(format_elapsed(status.elapsed, subsecond=False))
        parts.append("injectable" if status.injectable else "starting up")
        if status.queued:
            parts.append(f"{status.queued} queued")
        lines.append(summary_row(RUNNING_ICON, "Running", parts))
    else:
        lines.append(summary_row(IDLE_ICON, "Idle", []))
    if status.awaiting_approval:
        # The likeliest reason a turn looks stuck, so it gets its own line.
        lines.append(summary_row(BLOCKED_ICON, "Waiting on your approval", []))
    return "<br>".join(lines)


def _context_block(status: ScopeStatus) -> str:
    """Where the agent works, what it runs as, and which backend serves it."""
    override = " _(override)_"
    return "<br>".join([
        f"{DIRECTORY_ICON} `{status.ctx.directory}`",
        f"{MODEL_ICON} `{effective_model(status.ctx, status.config)}`"
        + (override if status.model_overridden else ""),
        f"{EFFORT_ICON} `{status.ctx.effort or 'default'}`"
        + (override if status.effort_overridden else ""),
        f"{BACKEND_ICON} `{effective_backend(status.ctx, status.config)}`",
    ])


def _sandbox_block(sandbox: SandboxSnapshot) -> str:
    """Sandbox state, then one row per forward this conversation opened."""
    lines = [summary_row(
        SANDBOX_ICON, "Sandbox", [f"`{sandbox.backend}`", sandbox.state],
    )]
    for fwd in sandbox.forwards:
        note = f" — {escape_rich_inline(fwd.description)}" if fwd.description else ""
        lines.append(
            f"{FORWARD_ICON} `127.0.0.1:{fwd.host_port}` → "
            f"guest `{fwd.guest_port}`{note}"
        )
    return "<br>".join(lines)


def _approvals_block(approvals: SessionApprovals) -> str | None:
    """What this session has been told to stop asking about."""
    bits: list[str] = []
    if approvals.all_edits:
        bits.append("all edits")
    if approvals.tool_rules:
        bits.append(plural(len(approvals.tool_rules), "tool rule"))
    if approvals.directories:
        bits.append(plural(len(approvals.directories), "extra dir"))
    if not bits:
        return None
    return summary_row(APPROVED_ICON, "Auto-approved", bits)


def task_facts(task: TrackedTask, now: float) -> tuple[str, str, str, str]:
    """``(short id, type, description, elapsed)`` for one background task.

    Shared with ``/tasks`` so the id truncation, the two defaults and the
    elapsed format are decided once even though the two commands lay the
    fields out differently.
    """
    return (
        task.task_id[:12],
        escape_rich_inline(task.task_type or "unknown"),
        escape_rich_inline(task.description or "No description"),
        format_elapsed(now - task.started_at, subsecond=False),
    )


def _task_blocks(tasks: list[TrackedTask], now: float) -> list[str]:
    """One paragraph per task: header line, then description.

    A four-column table wraps every cell to two or three lines on a phone,
    so each task gets the full width instead.
    """
    if not tasks:
        return []
    blocks = [f"**Background tasks ({len(tasks)})**"]
    for task in tasks:
        short_id, kind, description, elapsed = task_facts(task, now)
        blocks.append(f"`{short_id}` · {kind} · {elapsed}<br>{description}")
    return blocks


def render_status_card(
    status: ScopeStatus, now: float,
) -> tuple[str, InlineKeyboardMarkup]:
    """The ``/status`` card for a scope with a project bound."""
    blocks = [
        f"**Status** · `{status.context_name}`",
        _activity_block(status),
        _context_block(status),
    ]
    if status.sandbox is not None:
        blocks.append(_sandbox_block(status.sandbox))
    approvals = _approvals_block(status.approvals)
    if approvals:
        blocks.append(approvals)

    # Full id in a code span: Telegram makes monospace tap-to-copy, and a
    # truncated session id copies to nothing usable.
    blocks.append(
        f"{SESSION_ICON} `{status.session_id}`" if status.session_id
        else f"{SESSION_ICON} No session yet"
    )
    blocks.extend(_task_blocks(status.tasks, now))

    return "\n\n".join(blocks), status_keyboard(status.running)


def render_unbound_card(
    config: Config, no_context_text: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """The card for a scope with no project.

    Which projects exist and how to pick one is exactly what the user asked
    /status for, so the command answers rather than bailing.
    """
    lines = ["**Status** · no project picked", "", escape_rich(no_context_text)]
    if config.contexts:
        # Reaching here means default_context is unset or names a context
        # that is gone, so there is no default worth naming — only the count
        # of projects /context has to offer.
        lines.extend(["", f"{plural(len(config.contexts), 'project')} configured"])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{DIRECTORY_ICON} Pick a project", callback_data=STATUS_CONTEXT,
        ),
    ]])
    return "\n".join(lines), keyboard


# ── The pinned card ──


def _format_token_count(count: int) -> str:
    """Format a token count as a human-readable string (e.g. 12.3k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def build_pinned_status(
    ctx_name: str,
    ctx: ContextConfig,
    config: Config,
    model_usage: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> str:
    """Build the pinned status message body.

    Rows that belong together are joined with ``<br>``: a bare newline inside
    a rich-message paragraph collapses to a space, which runs the directory
    into the model and the context size into the cost.
    """
    blocks = [
        "<br>".join([
            f"{PIN_ICON} **Active context:** `{ctx_name}`",
            escape_rich(ctx.description),
        ]),
    ]
    lines = [
        f"{DIRECTORY_ICON} `{ctx.directory}`",
        f"{MODEL_ICON} `{effective_model(ctx, config)}`",
    ]
    if ctx.effort:
        lines.append(f"{EFFORT_ICON} **Effort:** `{ctx.effort}`")
    blocks.append("<br>".join(lines))

    usage_lines: list[str] = []

    # Context window usage from per-turn API usage (the last assistant
    # message).  input_tokens + cache tokens = current context size.
    if turn_usage:
        context_window = _DEFAULT_CONTEXT_LIMIT
        if model_usage:
            first_model = next(iter(model_usage.values()))
            context_window = first_model.get("contextWindow", _DEFAULT_CONTEXT_LIMIT)

        # Per-turn usage from the API: input_tokens is non-cached
        # input, plus the two cache buckets = total context size.
        total_tokens = (
            turn_usage.get("input_tokens", 0)
            + turn_usage.get("cache_creation_input_tokens", 0)
            + turn_usage.get("cache_read_input_tokens", 0)
        )

        total_str = _format_token_count(total_tokens)
        limit_str = _format_token_count(context_window)
        pct = min(total_tokens / context_window * 100, 100) if context_window > 0 else 0

        usage_lines.append(
            f"\U0001f4ca **Context:** {total_str} / {limit_str} "
            f"({pct:.0f}%)"
        )

    if model_usage:
        total_cost = sum(m.get("costUSD", 0) for m in model_usage.values())
        if total_cost > 0:
            usage_lines.append(f"\U0001f4b0 **Cost:** ${total_cost:.4f}")

    if usage_lines:
        blocks.append("<br>".join(usage_lines))

    if todos:
        lines = ["\U0001f4dd **Tasks:**", ""]
        # Cap at 15 items to avoid hitting Telegram's message length limit.
        display_todos = todos[:15]
        for todo in display_todos:
            todo_status = todo.get("status", "pending")
            content = escape_rich_inline(todo.get("content", ""))
            if todo_status == "completed":
                lines.append(f"- [x] ~~{content}~~")
            elif todo_status == "in_progress":
                lines.append(f"- [ ] **{content}**")
            else:
                lines.append(f"- [ ] {content}")
        remaining = len(todos) - len(display_todos)
        if remaining > 0:
            lines.append(f"\n*...and {remaining} more*")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
