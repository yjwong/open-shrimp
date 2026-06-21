"""The ``BackendPolicy`` protocol — per-backend tool taxonomy and rendering.

The orchestration files (``hooks.py``, ``stream.py``,
``handlers/approval.py``) dispatch through this protocol; each backend's
``policy.py`` module owns its native tool vocabulary, param keys, and
rendering.  No SDK imports — this is a pure structural contract sitting
alongside ``backend/protocol.py``.

See ``docs/tool-taxonomy-consolidation-plan.md`` for the motivation and
the per-method semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from telegram import InlineKeyboardButton

if TYPE_CHECKING:
    from open_shrimp.db import ChatScope
    from open_shrimp.hooks import ApprovalRule


@dataclass
class ApprovalKeyboardExtras:
    """Per-tool extras the approval-keyboard builder consumes.

    The orchestration code in ``handlers/approval.py`` owns the
    ``Approve`` / ``Deny`` core and the session-dir button; the policy
    contributes the per-tool decorations: an optional row inserted above
    the primary row (used by ExitPlanMode's "View plan" Mini App button),
    a "Show prompt" button merged into the primary row (Agent), a
    session-scoped row with tool-specific buttons ("Accept all edits",
    "Allow & remember: <prefix> *"), and the future objects to register
    so the corresponding callback data resolves the approval future.
    """

    #: Extra row inserted before the primary [Approve][Deny] row.  Used by
    #: ExitPlanMode's "View plan" Mini App button.
    pre_primary_rows: list[list[InlineKeyboardButton]] = field(
        default_factory=list,
    )
    #: Buttons merged into the primary row before [Approve][Deny].  Used by
    #: Agent's "Show prompt" button.
    primary_row_extras: list[InlineKeyboardButton] = field(default_factory=list)
    #: Session-scoped row appended below the primary row.  Holds tool-
    #: specific buttons like "Accept all edits" and "Allow & remember: git *".
    session_row: list[InlineKeyboardButton] = field(default_factory=list)
    #: Callback-data strings the orchestration must register against the
    #: approval future so clicks resolve it.  Each entry is a callback-data
    #: string that should be wired to the same future as ``approve:<id>``.
    future_callback_data: list[str] = field(default_factory=list)
    #: True if this policy contributed an "Accept all <Tool>" button via
    #: the orchestration's generic blanket-accept mechanism.  When True,
    #: the orchestration registers the future under the generated token
    #: and seeds ``_pending_tool_approvals``.
    use_blanket_accept_all: bool = False


@runtime_checkable
class BackendPolicy(Protocol):
    """Per-backend tool taxonomy and rendering.

    The orchestration files dispatch through this protocol; each backend's
    policy module owns its native tool vocabulary, param keys, and
    rendering.
    """

    # --- categorical queries (hooks.py) ---

    def is_path_scoped(self, tool_name: str) -> bool:
        """True if this tool's input contains a filesystem path argument
        that should drive path-scoped auto-approval."""
        ...

    def is_mutating(self, tool_name: str) -> bool:
        """True if this tool mutates the filesystem (Edit, Write, …)."""
        ...

    def is_file_targeted(self, tool_name: str) -> bool:
        """True if the tool's path arg points at a file (not a dir).

        Drives the parent-dir suggestion for "Allow <dir>/ this session"."""
        ...

    def extract_path(
        self, tool_name: str, tool_input: dict[str, Any], cwd: str,
    ) -> str | None:
        """The filesystem path the tool will touch, or None if not
        path-scoped.  Falls back to ``cwd`` for tools whose path is
        optional (Glob, Grep on SDK)."""
        ...

    def suggested_session_dir(
        self, tool_name: str, tool_input: dict[str, Any],
    ) -> str | None:
        """Directory to suggest for "Allow <dir>/ this session".

        For file-targeted tools, this is the parent of the target file;
        for directory-targeted tools, the path itself.  Returns None for
        tools with no meaningful path."""
        ...

    def is_safe_for_accept_edits_bash(
        self, command: str, approved_dirs: list[str],
    ) -> bool:
        """SDK-only safe-Bash allowlist for accept-all-edits mode.

        OpenCode returns False (no equivalent semantic)."""
        ...

    def container_auto_approve(self, tool_name: str) -> bool:
        """True if the tool should auto-approve inside a sandbox.

        SDK: True for Bash and Monitor; OpenCode: True for bash (no
        Monitor equivalent)."""
        ...

    def multi_file_mutating(self, tool_name: str) -> bool:
        """True for mutating tools that carry their own multi-file envelope
        (OpenCode: ``apply_patch``).  Such tools cannot be modelled by the
        single-path ``extract_path`` interface; the orchestration gives
        them their own branch.  SDK returns ``False`` for every tool."""
        ...

    def multi_file_paths_within(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        directories: list[str],
    ) -> bool:
        """For multi-file-mutating tools: True iff every path the tool
        will touch resolves inside one of ``directories``.  Empty / no
        paths returns ``False`` — refuse to blanket-allow when the
        envelope yielded nothing parseable."""
        ...

    def auto_approved_at_session_start(self) -> list[str]:
        """Tool names to seed into the backend's session-start
        auto-approve list (SDK: ``allowedTools``).

        These are tools whose Claude Code interactive default is
        auto-allow and that have no user-facing approval value in
        OpenShrimp's flow — async-task management, mode transitions, MCP
        discovery, ToolSearch.  Routed by ``client_manager.py`` when
        assembling the per-ChatScope allowed-tools list."""
        ...

    def handles_user_questions(self, tool_name: str) -> bool:
        """True for AskUserQuestion on SDK; False on OpenCode (whose
        ``question.asked`` event arm handles this natively)."""
        ...

    # --- streaming (stream.py) ---

    def suppress_notification(self, tool_name: str) -> bool:
        """True if the tool's inline blockquote notification should be
        suppressed because its output is shown directly (Bash output as
        code block, Write/Edit via dedicated diff messages)."""
        ...

    def summarize(
        self, tool_name: str, tool_input: dict[str, Any], cwd: str | None,
    ) -> str:
        """One-line summary for inline notifications."""
        ...

    def is_bash_like(self, tool_name: str) -> bool:
        """True for tools whose output should render as a collapsible
        "Show output" message (SDK: Bash + host_bash; OpenCode: bash +
        host_bash).  Drives the streaming Bash-button render path."""
        ...

    def is_host_bash(self, tool_name: str) -> bool:
        """True for the host-escape MCP tool's backend-specific wire
        name (SDK: ``mcp__openshrimp__host_bash``; OpenCode:
        ``openshrimp_host_bash``).  Drives the sudo-mode approval flow
        in ``hooks.py`` and the icon/label render in ``stream.py``."""
        ...

    def is_todo_write(self, tool_name: str) -> bool:
        """SDK: TodoWrite; OpenCode: todowrite.  Drives the pinned-
        message update callback."""
        ...

    def is_subagent_task(self, task_type: str | None) -> bool:
        """True if a task event of this ``task_type`` represents a
        sub-agent invocation whose descendant messages should be
        suppressed from the Telegram chat."""
        ...

    def host_bash_render(self) -> tuple[str, str]:
        """The (icon, label) pair used to render host_bash tool-result
        messages in ``stream.py``."""
        ...

    # --- approval UI (handlers/approval.py) ---

    def format_approval_text(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        """Markdown body of the approval prompt for this tool."""
        ...

    def format_auto_approved_diff(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        """Markdown body for the read-only auto-approved edit
        notification.  Mirrors ``format_approval_text`` but is used when
        the tool already auto-approved and we only want the user to see
        the diff."""
        ...

    def format_expanded_prompt(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str:
        """Markdown body for the "expanded prompt" view of an Agent-like
        tool — shown after the user clicks "Show prompt"."""
        ...

    def approval_keyboard_extras(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        base_url: str | None,
        *,
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        bot_token: str,
        is_private_chat: bool,
    ) -> ApprovalKeyboardExtras:
        """Per-tool keyboard customisations: "Accept all edits" row for
        Edit/Write; bash prefix-rule button; ExitPlanMode "View plan"
        Mini App row; Agent "Show prompt"."""
        ...

    def allows_blanket_accept_all(self, tool_name: str) -> bool:
        """True iff a generic "Accept all <Tool>" button is offered for
        this tool.  Path-scoped tools, Bash, Monitor, ExitPlanMode return
        False because they have their own dedicated approvals."""
        ...

    def bash_prefix_rule(self, command: str) -> str | None:
        """Extract a session-scoped prefix rule from a Bash command.

        Returns ``"git"`` for ``git status``, etc.  None for compound
        commands or non-extractable shapes."""
        ...

    # --- durable permission egress (handlers/approval.py, handlers/messages.py) ---

    async def persist_session_rule(
        self,
        rule: "ApprovalRule",
        *,
        directory: str,
        scope: "ChatScope",
    ) -> bool:
        """Persist *rule* to the backend's durable permission store.

        The SDK writes ``.claude/settings.local.json`` under *directory*;
        OpenCode patches the project's config permission via the live
        client (looked up by *scope*).  Returns True if the rule was
        accepted (newly added), False if already present, unsupported by
        the policy, or the egress failed.
        """
        ...

    async def load_persistent_rules(
        self, *, directory: str,
    ) -> list["ApprovalRule"]:
        """Return durable approval rules to seed the session-scoped cache.

        SDK reads from ``.claude/settings.local.json``; OpenCode returns
        ``[]`` (durable rules arrive through the ``permission.asked.always``
        event arm)."""
        ...


__all__ = [
    "ApprovalKeyboardExtras",
    "BackendPolicy",
]
