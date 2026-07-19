"""The Claude Agent SDK's tool taxonomy and rendering.

This module owns the SDK's native tool vocabulary, param keys, and
per-tool rendering.  It implements ``BackendPolicy`` and is consumed by
the orchestration files (``hooks.py``, ``stream.py``,
``handlers/approval.py``) through that protocol.

See ``docs/tool-taxonomy-consolidation-plan.md`` for the design.
"""

from __future__ import annotations

import difflib
import os
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton

from open_shrimp.backend.policy import ApprovalKeyboardExtras
from open_shrimp.web_app_button import make_web_app_button

if TYPE_CHECKING:
    from open_shrimp.db import ChatScope
    from open_shrimp.hooks import ApprovalRule


# ---------------------------------------------------------------------------
# Tool sets — the SDK's native vocabulary
# ---------------------------------------------------------------------------

#: Tools that access the filesystem, mapped to the input key(s) containing
#: the path to check.  Each value is a list of keys to try (first match wins).
_PATH_SCOPED_TOOLS: dict[str, list[str]] = {
    "Read": ["file_path"],
    "Write": ["file_path"],
    "Edit": ["file_path"],
    "NotebookEdit": ["notebook_path"],
    "Glob": ["path"],     # optional; defaults to cwd when absent
    "Grep": ["path"],     # optional; defaults to cwd when absent
}

#: Mutating file-access tools that require explicit approval even when the
#: target path is within the context working directory.  Read-only tools
#: (Read, Glob, Grep) are still auto-approved within cwd.
_MUTATING_PATH_TOOLS: set[str] = {"Edit", "Write", "NotebookEdit"}

#: Path-scoped tools whose path argument identifies a single file (vs a
#: directory).  For these, the parent directory is the right granularity
#: when suggesting a session-wide approval.
_FILE_TARGETED_PATH_TOOLS: set[str] = {"Read", "Edit", "Write", "NotebookEdit"}

#: Tools whose blockquote notifications are suppressed because their output
#: is shown directly (Bash output as code block, Write/Edit via dedicated
#: diff messages, NotebookEdit via the Edit-shaped affordances).  TaskList
#: and TaskGet are read-only checklist bookkeeping — pure noise next to the
#: pinned checklist message.
_SUPPRESS_NOTIFICATION_TOOLS: set[str] = {
    "Bash", "Edit", "Write", "NotebookEdit", "TaskList", "TaskGet",
}

#: The checklist tools that mutate the session checklist.  Incremental — no
#: tool input carries the full list, so each call triggers a re-read of the
#: CLI's on-disk task store (``task_checklist.read_checklist``) to refresh
#: the pinned message.  The read-only ``TaskList``/``TaskGet`` are excluded:
#: they cannot change the store, and the turn-end read already reconciles
#: out-of-band (subagent) changes.
_CHECKLIST_TOOLS: set[str] = {"TaskCreate", "TaskUpdate"}

#: Tools auto-approved inside a sandbox (Bash + Monitor both run arbitrary
#: shell commands; the sandbox provides the safety boundary).
_CONTAINER_AUTO_APPROVE_TOOLS: set[str] = {"Bash", "Monitor"}

#: Tools whose Claude Code interactive default is auto-allow and that have
#: no user-facing approval value in OpenShrimp's flow.  Four groups:
#: async-task management, mode transitions, registry/discovery, and skill
#: invocation.
#:
#: ExitPlanMode is *not* in this set — it keeps its "View plan" Mini App
#: approval row, since the user reviewing the plan is the entire point.
_AUTO_APPROVED_AT_SESSION_START: list[str] = [
    # Session checklist — the model's own task list.
    "TaskCreate",
    "TaskGet",
    "TaskUpdate",
    "TaskList",
    # Async task management — managing background tasks the model spawned.
    "TaskStop",
    "TaskOutput",
    # Mode transitions — entering/leaving plan or worktree modes.
    "EnterPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    # Registry / discovery — listing tools and MCP resources.
    "ToolSearch",
    "ListMcpResources",
    "ReadMcpResource",
    # Skill invocation — a bare Skill entry allows every skill (the same
    # mechanism as the SDK's ``skills="all"`` option).  Loading a skill
    # only injects its prompt; every tool call the skill then makes
    # still goes through the session's normal approval policy.
    "Skill",
]

#: Fully-qualified name of the host_bash MCP tool — the SDK's MCP server
#: prefix is ``mcp__<server>__<tool>``.  Used in several places to
#: special-case the host-escape path.
HOST_BASH_TOOL_NAME = "mcp__openshrimp__host_bash"

#: Fully-qualified name of the host_monitor MCP tool — the streaming
#: host-escape sibling of host_bash.  Routed through the same fresh-approval
#: pre-check as host_bash.
HOST_MONITOR_TOOL_NAME = "mcp__openshrimp__host_monitor"

#: Bash commands that are auto-approved when "accept all edits" is active.
#: Mirrors Claude Code's acceptEdits mode allowlist — these are common
#: file-manipulation commands that complement Edit/Write auto-approval.
_ACCEPT_EDITS_BASH_COMMANDS: set[str] = {
    "mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed", "chmod",
}

#: Prefixes to skip when extracting the bash command name (e.g. "sudo git").
_BASH_SKIP_PREFIXES = {"sudo", "env", "nohup", "nice", "ionice", "time", "strace"}


# ---------------------------------------------------------------------------
# Helpers — internal
# ---------------------------------------------------------------------------


def _relative_path(path: str, cwd: str | None) -> str:
    """Return *path* relative to *cwd* when it lives under that directory."""
    if not cwd or not path:
        return path
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:
        # On Windows, relpath raises ValueError for paths on different drives.
        return path
    if rel.startswith(".."):
        return path
    return rel


def _is_path_within_directory(path: str, directory: str) -> bool:
    """Check if a resolved path is within the given directory."""
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def _extract_bash_base_command(command: str) -> str | None:
    cmd = command.strip()
    if not cmd:
        return None
    base = cmd.split()[0]
    return base.rsplit("/", 1)[-1] or None


def _extract_bash_path_args(command: str) -> list[str]:
    words = command.strip().split()
    if len(words) <= 1:
        return []
    return [w for w in words[1:] if not w.startswith("-")]


def _is_dangerous_rm_target(path: str) -> bool:
    if path == "*" or path.endswith("/*"):
        return True
    normalized = path.rstrip("/") or "/"
    if normalized == "/":
        return True
    home = os.path.expanduser("~")
    if os.path.realpath(normalized) == os.path.realpath(home):
        return True
    parent = os.path.dirname(normalized)
    if parent == "/":
        return True
    return False


def _is_single_subcommand_safe(
    subcommand: str, approved_dirs: list[str],
) -> bool:
    base = _extract_bash_base_command(subcommand)
    if base is None or base not in _ACCEPT_EDITS_BASH_COMMANDS:
        return False

    path_args = _extract_bash_path_args(subcommand)

    for arg in path_args:
        if "$" in arg or "`" in arg or "~" in arg or "%" in arg:
            return False
        if base in ("rm", "rmdir") and _is_dangerous_rm_target(arg):
            return False

    for arg in path_args:
        if "*" in arg or "?" in arg:
            return False
        resolved = os.path.realpath(arg)
        if not any(
            _is_path_within_directory(resolved, d)
            for d in approved_dirs
        ):
            return False

    return True


# Escape function for MarkdownV2 (the SDK rendering uses MarkdownV2).  Kept
# private to this module so the policy doesn't reach into handlers/utils.
_MDV2_ESCAPE = "_*[]()~`>#+-=|{}.!"


def _escape_mdv2(text: str) -> str:
    escaped: list[str] = []
    for ch in text:
        if ch in _MDV2_ESCAPE:
            escaped.append("\\")
        escaped.append(ch)
    return "".join(escaped)


# ---------------------------------------------------------------------------
# Per-tool summary renderers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters with a trailing ellipsis."""
    return text[:limit] + ("..." if len(text) > limit else "")


def _summarize(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None,
) -> str:
    """Extract a brief summary from tool input for notifications."""
    if tool_name == "Read":
        return _relative_path(tool_input.get("file_path", ""), cwd)
    if tool_name == "Glob":
        return tool_input.get("pattern", "")
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        if path:
            return f"{pattern} in {_relative_path(path, cwd)}"
        return pattern
    if tool_name == "Bash":
        return _truncate(tool_input.get("command", ""), 80)
    if tool_name == "Write" or tool_name == "Edit":
        return _relative_path(tool_input.get("file_path", ""), cwd)
    if tool_name == "NotebookEdit":
        return _relative_path(tool_input.get("notebook_path", ""), cwd)
    if tool_name == "LSP":
        return tool_input.get("command", "")
    if tool_name == "Agent":
        desc = tool_input.get("description", "")
        subagent = tool_input.get("subagent_type", "")
        label = f"({subagent}) " if subagent else ""
        return f"{label}{desc}" if desc else subagent
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            return questions[0].get(
                "header", questions[0].get("question", ""),
            )[:60]
        return "asking user"
    if tool_name == "TaskCreate":
        return _truncate(tool_input.get("subject", ""), 60)
    if tool_name == "TaskUpdate":
        task_id = tool_input.get("taskId", "?")
        status = tool_input.get("status")
        if status:
            return f"#{task_id} → {status}"
        changed = next(
            (k for k in ("subject", "description", "activeForm", "owner")
             if k in tool_input),
            None,
        )
        if changed:
            return f"#{task_id} {changed}"
        if "addBlocks" in tool_input or "addBlockedBy" in tool_input:
            return f"#{task_id} deps"
        return f"#{task_id}"
    if tool_name == "TaskGet":
        return f"#{tool_input.get('taskId', '?')}"
    if tool_name == "TaskList":
        return "list"
    if tool_name == "mcp__openshrimp__send_file":
        path = tool_input.get("file_path", "")
        basename = os.path.basename(path) if path else ""
        caption = tool_input.get("caption", "")
        if caption:
            return f"{basename} — {caption[:40]}"
        return basename
    # Generic: show first key's value
    for key, val in tool_input.items():
        if isinstance(val, str):
            return _truncate(val, 60)
    return ""


# ---------------------------------------------------------------------------
# Per-tool approval renderers
# ---------------------------------------------------------------------------


def _format_edit_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format an Edit (or NotebookEdit) tool call as a unified diff."""
    # NotebookEdit uses ``notebook_path`` instead of ``file_path``.
    file_path = tool_input.get("file_path") or tool_input.get(
        "notebook_path", "unknown",
    )
    file_path = _relative_path(file_path, cwd)
    # NotebookEdit uses ``new_source`` / ``old_source`` (notebook cells)
    # but most invocations still ship the legacy Edit shape too — try the
    # canonical Edit keys first, fall back to the notebook keys.
    old_string = tool_input.get("old_string") or tool_input.get(
        "old_source", "",
    )
    new_string = tool_input.get("new_string") or tool_input.get(
        "new_source", "",
    )

    escaped_path = _escape_mdv2(file_path)
    is_notebook = "notebook_path" in tool_input
    label = "NotebookEdit" if is_notebook else "Edit"
    header = f"✏️ *{label}:* `{escaped_path}`"

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, lineterm="",
    ))

    if diff_lines:
        diff_body = "\n".join(diff_lines[2:])
    else:
        diff_body = "(no diff)"

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

    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    escaped_cmd = _escape_mdv2(command)
    parts.append(f"```bash\n{escaped_cmd}\n```")

    return "\n\n".join(parts)


def _format_monitor_approval(tool_input: dict[str, Any]) -> str:
    """Format a Monitor tool call for the approval prompt."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    persistent = tool_input.get("persistent", False)

    parts: list[str] = []
    header = "\U0001f4e1 *Monitor*"
    if description:
        header = f"{header}: {_escape_mdv2(description)}"
    if persistent:
        header = f"{header} _\\(persistent\\)_"
    parts.append(header)

    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    parts.append(f"```bash\n{_escape_mdv2(command)}\n```")

    return "\n\n".join(parts)


def _format_write_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format a Write tool call for the approval prompt."""
    file_path = _relative_path(tool_input.get("file_path", "unknown"), cwd)
    content = tool_input.get("content", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\U0001f4dd *Write:* `{escaped_path}`"

    max_content_len = 4096 - 200
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n..."

    escaped_content = _escape_mdv2(content)
    return f"{header}\n\n```\n{escaped_content}\n```"


def _format_agent_approval(
    tool_input: dict[str, Any], expanded: bool = False,
) -> str:
    """Format an Agent tool call for the approval prompt."""
    description = tool_input.get("description", "")
    subagent_type = tool_input.get("subagent_type", "")
    prompt = tool_input.get("prompt", "")

    parts: list[str] = []

    if subagent_type:
        parts.append(
            f"\U0001f916 *Agent* \\({_escape_mdv2(subagent_type)}\\)",
        )
    else:
        parts.append("\U0001f916 *Agent*")

    if description:
        parts.append(_escape_mdv2(description))

    if expanded and prompt:
        max_prompt_len = 4096 - 300
        display_prompt = prompt
        if len(display_prompt) > max_prompt_len:
            display_prompt = display_prompt[:max_prompt_len] + "\n..."
        parts.append(f"```\n{_escape_mdv2(display_prompt)}\n```")

    return "\n\n".join(parts)


def _format_plan_approval(tool_input: dict[str, Any]) -> str:
    """Format an ExitPlanMode tool call for the approval prompt."""
    plan = tool_input.get("plan", "")
    preview = ""
    for line in plan.splitlines():
        stripped = line.strip().lstrip("# ").strip()
        if stripped:
            preview = stripped
            break
    if len(preview) > 80:
        preview = preview[:77] + "..."
    header = "\U0001f4cb *Plan*"
    if preview:
        header += f": {_escape_mdv2(preview)}"
    return header


def _format_generic_approval(
    tool_name: str, tool_input: dict[str, Any],
) -> str:
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
# Bash prefix extraction
# ---------------------------------------------------------------------------


def _extract_bash_prefix(command: str) -> str | None:
    """Extract the primary command name from a bash command string."""
    cmd = command.strip()
    if not cmd or cmd.startswith("(") or cmd.startswith("{"):
        return None

    for sep in ("&&", "||", ";"):
        cmd = cmd.split(sep, 1)[0].strip()

    cmd = cmd.split("|", 1)[0].strip()

    words = cmd.split()
    if not words:
        return None

    idx = 0
    in_prefix = True
    while idx < len(words) and in_prefix:
        word = words[idx]
        if word in _BASH_SKIP_PREFIXES:
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
                if idx < len(words) and not words[idx].startswith("-"):
                    idx += 1
            continue
        if "=" in word and idx > 0:
            idx += 1
            continue
        in_prefix = False

    if idx >= len(words):
        return None

    prefix = words[idx]
    if "/" in prefix and not prefix.startswith("./"):
        return None

    return prefix


# ---------------------------------------------------------------------------
# The policy class
# ---------------------------------------------------------------------------


class ClaudeSdkPolicy:
    """The Claude Agent SDK's ``BackendPolicy`` implementation."""

    # --- categorical queries (hooks.py) ---

    def is_path_scoped(self, tool_name: str) -> bool:
        return tool_name in _PATH_SCOPED_TOOLS

    def is_mutating(self, tool_name: str) -> bool:
        return tool_name in _MUTATING_PATH_TOOLS

    def is_file_targeted(self, tool_name: str) -> bool:
        return tool_name in _FILE_TARGETED_PATH_TOOLS

    def extract_path(
        self, tool_name: str, tool_input: dict[str, Any], cwd: str,
    ) -> str | None:
        keys = _PATH_SCOPED_TOOLS.get(tool_name)
        if keys is None:
            return None
        for key in keys:
            value = tool_input.get(key)
            if value is not None:
                return str(value)
        # Glob and Grep default to cwd when no path is provided
        if tool_name in ("Glob", "Grep"):
            return cwd
        return None

    def suggested_session_dir(
        self, tool_name: str, tool_input: dict[str, Any],
    ) -> str | None:
        keys = _PATH_SCOPED_TOOLS.get(tool_name)
        if keys is None:
            return None
        for key in keys:
            value = tool_input.get(key)
            if value is None:
                continue
            real = os.path.realpath(str(value))
            if tool_name in _FILE_TARGETED_PATH_TOOLS:
                return os.path.dirname(real) or None
            return real
        return None

    def is_safe_for_accept_edits_bash(
        self, command: str, approved_dirs: list[str],
    ) -> bool:
        """Tree-sitter check on a Bash command for accept-all-edits mode.

        Imports ``bash_parse`` lazily so the policy module doesn't pull
        in tree-sitter at import time.
        """
        from open_shrimp.bash_parse import (
            check_compound_safety,
            parse_command,
        )

        result = parse_command(command)
        if result.kind != "simple":
            return False

        subcommands = [cmd.text for cmd in result.commands]
        if not subcommands:
            return False

        safety_reason = check_compound_safety(subcommands)
        if safety_reason is not None:
            return False

        return all(
            _is_single_subcommand_safe(sub, approved_dirs)
            for sub in subcommands
        )

    def container_auto_approve(self, tool_name: str) -> bool:
        return tool_name in _CONTAINER_AUTO_APPROVE_TOOLS

    def multi_file_mutating(self, tool_name: str) -> bool:
        # The SDK has no multi-file mutating tool, so the orchestration's
        # multi-file branch stays dormant.
        return False

    def multi_file_paths_within(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        directories: list[str],
    ) -> bool:
        return False

    def auto_approved_at_session_start(self) -> list[str]:
        return list(_AUTO_APPROVED_AT_SESSION_START)

    def handles_user_questions(self, tool_name: str) -> bool:
        return tool_name == "AskUserQuestion"

    # --- streaming (stream.py) ---

    def suppress_notification(self, tool_name: str) -> bool:
        return tool_name in _SUPPRESS_NOTIFICATION_TOOLS

    def summarize(
        self, tool_name: str, tool_input: dict[str, Any], cwd: str | None,
    ) -> str:
        return _summarize(tool_name, tool_input, cwd)

    def is_bash_like(self, tool_name: str) -> bool:
        return tool_name in ("Bash", HOST_BASH_TOOL_NAME)

    def is_host_bash(self, tool_name: str) -> bool:
        return tool_name == HOST_BASH_TOOL_NAME

    def is_host_escape(self, tool_name: str) -> bool:
        return tool_name in (HOST_BASH_TOOL_NAME, HOST_MONITOR_TOOL_NAME)

    def is_checklist_tool(self, tool_name: str) -> bool:
        return tool_name in _CHECKLIST_TOOLS

    def checklist_snapshot(
        self, tool_name: str, tool_input: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        return None

    def is_subagent_task(self, task_type: str | None) -> bool:
        return task_type in ("local_agent", "remote_agent")

    def host_bash_render(self) -> tuple[str, str]:
        return ("\U0001f513", "host_bash")

    # --- approval UI (handlers/approval.py) ---

    def format_approval_text(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        if tool_name == "Edit" or tool_name == "NotebookEdit":
            return _format_edit_approval(tool_input, cwd=cwd)
        if tool_name == "Bash":
            return _format_bash_approval(tool_input)
        if tool_name == "Monitor":
            return _format_monitor_approval(tool_input)
        if tool_name == "Write":
            return _format_write_approval(tool_input, cwd=cwd)
        if tool_name == "Agent":
            return _format_agent_approval(tool_input, expanded=False)
        if tool_name == "ExitPlanMode":
            return _format_plan_approval(tool_input)
        return _format_generic_approval(tool_name, tool_input)

    def format_auto_approved_diff(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        if tool_name == "Edit" or tool_name == "NotebookEdit":
            return _format_edit_approval(tool_input, cwd=cwd)
        if tool_name == "Write":
            return _format_write_approval(tool_input, cwd=cwd)
        return _format_generic_approval(tool_name, tool_input)

    def format_expanded_prompt(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str:
        return _format_agent_approval(tool_input, expanded=True)

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
        """Build the per-tool keyboard extras for the SDK's tool surface."""
        from open_shrimp.handlers.state import _pending_agent_inputs

        extras = ApprovalKeyboardExtras()

        # Agent: "Show prompt" merged into the primary row.
        if tool_name == "Agent":
            show_prompt_data = f"show_prompt:{tool_use_id}"
            _pending_agent_inputs[tool_use_id] = tool_input
            extras.primary_row_extras.append(
                InlineKeyboardButton(
                    "Show prompt", callback_data=show_prompt_data,
                ),
            )

        # Edit / Write / NotebookEdit: "Accept all edits" session button.
        if tool_name in ("Edit", "Write", "NotebookEdit"):
            accept_all_data = f"accept_all_edits:{tool_use_id}"
            extras.session_row.append(
                InlineKeyboardButton(
                    "Accept all edits", callback_data=accept_all_data,
                ),
            )
            extras.future_callback_data.append(accept_all_data)

        # Bash: prefix-specific persistent rule.
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            prefix = self.bash_prefix_rule(command)
            if prefix:
                accept_prefix_data = (
                    f"accept_bash_pfx:{tool_use_id}:{prefix}"
                )
                if len(accept_prefix_data.encode()) <= 64:
                    extras.session_row.append(InlineKeyboardButton(
                        f"Allow & remember: {prefix} *",
                        callback_data=accept_prefix_data,
                    ))
                    extras.future_callback_data.append(accept_prefix_data)

        # Blanket "Accept all <Tool>" for non-path-scoped non-bash tools.
        if self.allows_blanket_accept_all(tool_name):
            extras.use_blanket_accept_all = True

        # ExitPlanMode: "View plan" Mini App row.
        if tool_name == "ExitPlanMode" and base_url:
            plan = tool_input.get("plan", "")
            if plan:
                from open_shrimp.preview.api import store_ephemeral_content

                content_id = store_ephemeral_content(
                    "Plan", plan,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    tool_use_id=tool_use_id,
                )
                thread_param = (
                    f"&thread_id={thread_id}"
                    if thread_id is not None
                    else ""
                )
                app_url = (
                    f"{base_url}/preview/"
                    f"?content_id={content_id}"
                    f"&chat_id={chat_id}"
                    f"{thread_param}"
                )
                extras.pre_primary_rows.append([make_web_app_button(
                    "\U0001f4cb View plan",
                    app_url,
                    chat_id=chat_id,
                    user_id=user_id,
                    bot_token=bot_token,
                    is_private_chat=is_private_chat,
                )])

        return extras

    def allows_blanket_accept_all(self, tool_name: str) -> bool:
        # Path-scoped tools, Bash, Monitor, and ExitPlanMode have their own
        # dedicated approvals — no generic "Accept all <Tool>" button.
        _no_accept_all = (
            set(_PATH_SCOPED_TOOLS) | {"ExitPlanMode", "Bash", "Monitor"}
        )
        return tool_name not in _no_accept_all

    def bash_prefix_rule(self, command: str) -> str | None:
        return _extract_bash_prefix(command)

    # --- durable permission egress ---

    async def persist_session_rule(
        self,
        rule: "ApprovalRule",
        *,
        directory: str,
        scope: "ChatScope",
    ) -> bool:
        """Persist *rule* to ``.claude/settings.local.json`` under *directory*.

        ``scope`` is unused for the SDK — Claude Code's durable rules live
        in the project-local settings file, not in any in-memory session
        state.
        """
        del scope
        from open_shrimp.settings_local import save_persistent_rule

        return await save_persistent_rule(directory, rule)

    async def load_persistent_rules(
        self, *, directory: str,
    ) -> list["ApprovalRule"]:
        """Load durable approval rules from ``.claude/settings.local.json``."""
        from open_shrimp.settings_local import load_persistent_rules

        return await load_persistent_rules(directory)


__all__ = [
    "ClaudeSdkPolicy",
    "HOST_BASH_TOOL_NAME",
]
