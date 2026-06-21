"""The OpenCode backend's tool taxonomy and rendering.

This module owns OpenCode's native tool vocabulary (lowercase: ``bash``,
``read``, ``edit``, …), its camelCase param keys (``filePath``,
``oldString``, …), the multi-file ``apply_patch`` envelope handling, and
the per-tool approval renderers.  It implements ``BackendPolicy``.

OpenCode's experimental tools (``plan_exit``, ``lsp``, ``repo_clone``,
``repo_overview``) are not handled — they are gated behind
``OPENCODE_EXPERIMENTAL_*`` environment flags OpenShrimp does not enable.
If a user opts in, those tools fall through to the orchestration's
generic deny-with-prompt path until this policy is extended.

The ``question`` tool is also not handled: its blocking lifecycle is
driven by the native ``question.asked`` SSE arm in
``backend/opencode/translate.py``, not through ``can_use_tool``.
"""

from __future__ import annotations

import difflib
import logging
import os
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton

from open_shrimp.backend.policy import ApprovalKeyboardExtras
from open_shrimp.web_app_button import make_web_app_button

if TYPE_CHECKING:
    from open_shrimp.db import ChatScope
    from open_shrimp.hooks import ApprovalRule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool sets — OpenCode's native vocabulary
# ---------------------------------------------------------------------------

#: Tools that access the filesystem, mapped to the input key(s) containing
#: the path to check.  ``apply_patch`` is intentionally absent — it carries
#: a multi-file envelope and is handled via ``multi_file_mutating``.
_PATH_SCOPED_TOOLS: dict[str, list[str]] = {
    "read": ["filePath"],
    "write": ["filePath"],
    "edit": ["filePath"],
    "glob": ["path"],     # optional; defaults to cwd when absent
    "grep": ["path"],     # optional; defaults to cwd when absent
}

#: Mutating file-access tools that require explicit approval even when the
#: target path is within the context working directory.
_MUTATING_PATH_TOOLS: set[str] = {"edit", "write"}

#: Path-scoped tools whose path argument identifies a single file (vs a
#: directory).
_FILE_TARGETED_PATH_TOOLS: set[str] = {"read", "edit", "write"}

#: Tools whose blockquote notifications are suppressed because their output
#: is shown directly (bash output as code block; edit/write/apply_patch via
#: dedicated diff messages).
_SUPPRESS_NOTIFICATION_TOOLS: set[str] = {"bash", "edit", "write", "apply_patch"}

#: Tools auto-approved inside a sandbox (bash runs arbitrary shell
#: commands; the sandbox provides the safety boundary).  OpenCode has no
#: Monitor equivalent.
_CONTAINER_AUTO_APPROVE_TOOLS: set[str] = {"bash"}

#: Fully-qualified name of the host_bash MCP tool — OpenCode's MCP server
#: prefix is ``<server>_<tool>`` (no ``mcp__`` prefix).
HOST_BASH_TOOL_NAME = "openshrimp_host_bash"

#: Prefixes to skip when extracting the bash command name.
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
        return path
    if rel.startswith(".."):
        return path
    return rel


def _is_path_within_directory(path: str, directory: str) -> bool:
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


_MDV2_ESCAPE = "_*[]()~`>#+-=|{}.!"


def _escape_mdv2(text: str) -> str:
    escaped: list[str] = []
    for ch in text:
        if ch in _MDV2_ESCAPE:
            escaped.append("\\")
        escaped.append(ch)
    return "".join(escaped)


# ---------------------------------------------------------------------------
# apply_patch envelope handling
# ---------------------------------------------------------------------------


_APPLY_PATCH_HEADERS: tuple[tuple[str, str], ...] = (
    ("add", "Add File: "),
    ("update", "Update File: "),
    ("delete", "Delete File: "),
    ("move", "Move to: "),
)


def _parse_apply_patch_files(patch_text: str) -> list[tuple[str, str]]:
    """Return ``(action, path)`` pairs from an apply_patch envelope.

    Recognises ``*** Add File: <path>``, ``*** Update File: <path>``,
    ``*** Delete File: <path>``, and ``*** Move to: <path>`` headers
    (see opencode's ``apply_patch.txt``).  Paths are returned verbatim
    — the caller decides whether to resolve against a cwd.
    """
    out: list[tuple[str, str]] = []
    for line in patch_text.splitlines():
        if not line.startswith("*** "):
            continue
        rest = line[4:]
        for action, prefix in _APPLY_PATCH_HEADERS:
            if rest.startswith(prefix):
                path = rest[len(prefix):].strip()
                if path:
                    out.append((action, path))
                break
    return out


_APPLY_PATCH_ACTION_ICONS = {"add": "+", "update": "~", "delete": "-", "move": ">"}

_ENVELOPE_MARKERS = ("*** Begin Patch", "*** End Patch", "*** End of File")


class _ApplyPatchFile:
    """One file's worth of an apply_patch envelope.

    ``new_path`` only differs from ``path`` for ``move`` (rename)
    operations.  ``body_lines`` are the raw hunk lines from the envelope.
    """

    __slots__ = ("action", "path", "new_path", "body_lines")

    def __init__(
        self,
        action: str,
        path: str,
        new_path: str | None,
        body_lines: list[str],
    ) -> None:
        self.action = action
        self.path = path
        self.new_path = new_path
        self.body_lines = body_lines


def _parse_apply_patch_envelope(patch_text: str) -> list[_ApplyPatchFile]:
    """Split an apply_patch envelope into per-file records with hunk bodies."""
    files: list[_ApplyPatchFile] = []
    current: _ApplyPatchFile | None = None

    for raw_line in patch_text.splitlines():
        line = raw_line
        if line in _ENVELOPE_MARKERS:
            continue

        if line.startswith("*** Add File: "):
            current = _ApplyPatchFile(
                "add", line[len("*** Add File: "):].strip(), None, [],
            )
            files.append(current)
            continue
        if line.startswith("*** Update File: "):
            current = _ApplyPatchFile(
                "update", line[len("*** Update File: "):].strip(), None, [],
            )
            files.append(current)
            continue
        if line.startswith("*** Delete File: "):
            current = _ApplyPatchFile(
                "delete", line[len("*** Delete File: "):].strip(), None, [],
            )
            files.append(current)
            continue
        if line.startswith("*** Move to: ") and current is not None:
            current.new_path = line[len("*** Move to: "):].strip()
            continue

        if current is not None and line and not line.startswith("*** "):
            current.body_lines.append(line)

    # A bare "Update File + Move to" with no hunk body is a pure rename.
    for f in files:
        if f.action == "update" and f.new_path and not f.body_lines:
            f.action = "move"

    return files


def _render_apply_patch_file(file: _ApplyPatchFile, cwd: str | None) -> str:
    """Render one envelope file as a unified-diff fragment."""
    rel = _relative_path(file.path, cwd)

    if file.action == "add":
        header = f"--- /dev/null\n+++ b/{rel}"
        body = "\n".join(file.body_lines)
        return f"{header}\n{body}" if body else header
    if file.action == "delete":
        return f"--- a/{rel}\n+++ /dev/null"
    if file.action == "move":
        new_rel = _relative_path(file.new_path or file.path, cwd)
        return f"rename from {rel}\nrename to {new_rel}"

    target_rel = _relative_path(file.new_path, cwd) if file.new_path else rel
    header = f"--- a/{rel}\n+++ b/{target_rel}"
    body = "\n".join(file.body_lines)
    return f"{header}\n{body}" if body else header


# ---------------------------------------------------------------------------
# Per-tool summary renderer
# ---------------------------------------------------------------------------


def _summarize(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None,
) -> str:
    """Extract a brief summary from tool input for notifications."""
    if tool_name == "read":
        return _relative_path(tool_input.get("filePath", ""), cwd)
    if tool_name == "glob":
        return tool_input.get("pattern", "")
    if tool_name == "grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        if path:
            return f"{pattern} in {_relative_path(path, cwd)}"
        return pattern
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if tool_name in ("write", "edit"):
        return _relative_path(tool_input.get("filePath", ""), cwd)
    if tool_name == "apply_patch":
        files = _parse_apply_patch_files(tool_input.get("patchText", ""))
        if not files:
            return "(empty patch)"
        if len(files) == 1:
            action, path = files[0]
            return f"{action} {_relative_path(path, cwd)}"
        return f"{len(files)} files"
    if tool_name == "agent":
        desc = tool_input.get("description", "")
        subagent = tool_input.get("subagent_type", "")
        label = f"({subagent}) " if subagent else ""
        return f"{label}{desc}" if desc else subagent
    if tool_name == "todowrite":
        todos = tool_input.get("todos", [])
        if not todos:
            return "clear all"
        completed = sum(
            1 for t in todos
            if isinstance(t, dict) and t.get("status") == "completed"
        )
        total = len(todos)
        return f"{completed}/{total} done"
    if tool_name in ("openshrimp_send_file", "mcp__openshrimp__send_file"):
        path = tool_input.get("file_path", "")
        basename = os.path.basename(path) if path else ""
        caption = tool_input.get("caption", "")
        if caption:
            return f"{basename} — {caption[:40]}"
        return basename
    # Generic: show first key's value
    for key, val in tool_input.items():
        if isinstance(val, str):
            s = val[:60]
            return s + ("..." if len(val) > 60 else "")
    return ""


# ---------------------------------------------------------------------------
# Per-tool approval renderers
# ---------------------------------------------------------------------------


def _format_edit_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format an edit tool call as a unified diff."""
    file_path = _relative_path(
        tool_input.get("filePath", "unknown"), cwd,
    )
    old_string = tool_input.get("oldString", "")
    new_string = tool_input.get("newString", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"✏️ *Edit:* `{escaped_path}`"

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


def _format_write_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    file_path = _relative_path(tool_input.get("filePath", "unknown"), cwd)
    content = tool_input.get("content", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\U0001f4dd *Write:* `{escaped_path}`"

    max_content_len = 4096 - 200
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n..."

    escaped_content = _escape_mdv2(content)
    return f"{header}\n\n```\n{escaped_content}\n```"


def _format_apply_patch_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    patch_text = tool_input.get("patchText", "")
    files = _parse_apply_patch_envelope(patch_text)
    file_count = len(files)

    parts: list[str] = []

    if file_count == 0:
        parts.append("\U0001fa84 *ApplyPatch*")
        max_body_len = 4096 - 400
        body = patch_text
        if len(body) > max_body_len:
            body = body[:max_body_len] + "\n..."
        parts.append(f"```diff\n{_escape_mdv2(body)}\n```")
        return "\n\n".join(parts)

    plural = "" if file_count == 1 else "s"
    parts.append(f"\U0001fa84 *ApplyPatch* \\({file_count} file{plural}\\)")

    if file_count > 1:
        summary_lines: list[str] = []
        for f in files:
            icon = _APPLY_PATCH_ACTION_ICONS.get(f.action, "?")
            if f.action == "move":
                summary_lines.append(
                    f"{icon} {_relative_path(f.path, cwd)} → "
                    f"{_relative_path(f.new_path or f.path, cwd)}"
                )
            elif f.action == "update" and f.new_path:
                summary_lines.append(
                    f"{icon} {_relative_path(f.path, cwd)} → "
                    f"{_relative_path(f.new_path, cwd)}"
                )
            else:
                summary_lines.append(f"{icon} {_relative_path(f.path, cwd)}")
        parts.append(
            f"```\n{_escape_mdv2(chr(10).join(summary_lines))}\n```",
        )

    max_body_len = 4096 - 400
    rendered = 0
    omitted = 0
    for f in files:
        fragment = _render_apply_patch_file(f, cwd)
        block = f"```diff\n{_escape_mdv2(fragment)}\n```"
        used = sum(len(p) for p in parts) + 2 * len(parts)
        if used + len(block) > max_body_len:
            if rendered == 0:
                budget = max(
                    0, max_body_len - used - len("```diff\n\n```\n..."),
                )
                truncated = _escape_mdv2(fragment)[:budget] + "\n..."
                parts.append(f"```diff\n{truncated}\n```")
                rendered += 1
            omitted = file_count - rendered
            break
        parts.append(block)
        rendered += 1

    if omitted > 0:
        more_plural = "" if omitted == 1 else "s"
        parts.append(
            f"_\\.\\.\\. {omitted} more file{more_plural} omitted_",
        )

    return "\n\n".join(parts)


def _format_agent_approval(
    tool_input: dict[str, Any], expanded: bool = False,
) -> str:
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


def _format_generic_approval(
    tool_name: str, tool_input: dict[str, Any],
) -> str:
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


class OpenCodePolicy:
    """The OpenCode backend's ``BackendPolicy`` implementation."""

    # --- categorical queries (hooks.py) ---

    def is_path_scoped(self, tool_name: str) -> bool:
        return tool_name in _PATH_SCOPED_TOOLS

    def is_mutating(self, tool_name: str) -> bool:
        # apply_patch joins edit/write — it carries its own envelope but
        # is unambiguously mutating.  Hooks routes it through the
        # multi-file branch, but ``is_mutating`` still answers True so
        # the streaming auto-approved-diff notification fires correctly.
        return tool_name in _MUTATING_PATH_TOOLS or tool_name == "apply_patch"

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
        if tool_name in ("glob", "grep"):
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
        # OpenCode has no equivalent allowlist semantic — accept-all-
        # edits on OpenCode only auto-approves the path-scoped editing
        # tools and apply_patch, not arbitrary safe Bash commands.
        return False

    def container_auto_approve(self, tool_name: str) -> bool:
        return tool_name in _CONTAINER_AUTO_APPROVE_TOOLS

    def auto_approved_at_session_start(self) -> list[str]:
        # OpenCode uses session-scoped category rules and the
        # ``permission.asked.always`` arm for the same purpose.  The
        # protocol method exists for symmetry; the OpenCode body is
        # intentionally empty.
        return []

    def handles_user_questions(self, tool_name: str) -> bool:
        # OpenCode's ``question`` tool is driven by the native
        # ``question.asked`` SSE arm in translate.py, not through
        # can_use_tool.  Returning False keeps this branch dormant.
        return False

    def multi_file_mutating(self, tool_name: str) -> bool:
        return tool_name == "apply_patch"

    def multi_file_paths_within(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        directories: list[str],
    ) -> bool:
        if tool_name != "apply_patch":
            return False
        patch_text = str(tool_input.get("patchText", ""))
        files = _parse_apply_patch_files(patch_text)
        if not files:
            return False
        resolved = [
            p if os.path.isabs(p) else os.path.join(cwd, p)
            for _, p in files
        ]
        return all(
            any(_is_path_within_directory(p, d) for d in directories)
            for p in resolved
        )

    # --- streaming (stream.py) ---

    def suppress_notification(self, tool_name: str) -> bool:
        return tool_name in _SUPPRESS_NOTIFICATION_TOOLS

    def summarize(
        self, tool_name: str, tool_input: dict[str, Any], cwd: str | None,
    ) -> str:
        return _summarize(tool_name, tool_input, cwd)

    def is_bash_like(self, tool_name: str) -> bool:
        return tool_name in ("bash", HOST_BASH_TOOL_NAME)

    def is_host_bash(self, tool_name: str) -> bool:
        return tool_name == HOST_BASH_TOOL_NAME

    def is_todo_write(self, tool_name: str) -> bool:
        return tool_name == "todowrite"

    def is_subagent_task(self, task_type: str | None) -> bool:
        return False

    def host_bash_render(self) -> tuple[str, str]:
        return ("\U0001f513", "host_bash")

    # --- approval UI (handlers/approval.py) ---

    def format_approval_text(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        if tool_name == "edit":
            return _format_edit_approval(tool_input, cwd=cwd)
        if tool_name == "bash":
            return _format_bash_approval(tool_input)
        if tool_name == "write":
            return _format_write_approval(tool_input, cwd=cwd)
        if tool_name == "apply_patch":
            return _format_apply_patch_approval(tool_input, cwd=cwd)
        if tool_name == "agent":
            return _format_agent_approval(tool_input, expanded=False)
        return _format_generic_approval(tool_name, tool_input)

    def format_auto_approved_diff(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str | None,
    ) -> str:
        if tool_name == "edit":
            return _format_edit_approval(tool_input, cwd=cwd)
        if tool_name == "write":
            return _format_write_approval(tool_input, cwd=cwd)
        if tool_name == "apply_patch":
            return _format_apply_patch_approval(tool_input, cwd=cwd)
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
        from open_shrimp.handlers.state import _pending_agent_inputs

        extras = ApprovalKeyboardExtras()

        # agent: "Show prompt" merged into the primary row.
        if tool_name == "agent":
            show_prompt_data = f"show_prompt:{tool_use_id}"
            _pending_agent_inputs[tool_use_id] = tool_input
            extras.primary_row_extras.append(
                InlineKeyboardButton(
                    "Show prompt", callback_data=show_prompt_data,
                ),
            )

        # edit / write / apply_patch: "Accept all edits" session button.
        if tool_name in ("edit", "write", "apply_patch"):
            accept_all_data = f"accept_all_edits:{tool_use_id}"
            extras.session_row.append(
                InlineKeyboardButton(
                    "Accept all edits", callback_data=accept_all_data,
                ),
            )
            extras.future_callback_data.append(accept_all_data)

        # bash: prefix-specific persistent rule.
        if tool_name == "bash":
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

        return extras

    def allows_blanket_accept_all(self, tool_name: str) -> bool:
        # Path-scoped tools, bash, and apply_patch (edit-flavoured) have
        # their own dedicated approvals — no generic "Accept all <Tool>"
        # button.
        _no_accept_all = (
            set(_PATH_SCOPED_TOOLS) | {"bash", "apply_patch"}
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
        """Persist *rule* via OpenCode's project-config permission API.

        Looks up the live ``OpenCodeClient`` for *scope* and calls
        ``patch_config_permission``.  *directory* is unused — OpenCode
        scopes durable rules by the live client's ``cwd``, set at
        ``connect()`` time.
        """
        del directory
        permission = _opencode_permission_for_rule(rule)
        if permission is None:
            return False
        key, pattern = permission

        from open_shrimp.client_manager import get_session

        session = get_session(scope)
        if session is None:
            logger.warning(
                "No active OpenCode session for durable permission patch: %s",
                scope,
            )
            return False
        client = session.client
        if not hasattr(client, "patch_config_permission"):
            logger.warning(
                "Active client for scope %s does not support patch_config_permission",
                scope,
            )
            return False
        try:
            await client.patch_config_permission({key: {pattern: "allow"}})
        except Exception:
            logger.exception(
                "Failed to patch OpenCode config permission for %s(%s)",
                key, pattern,
            )
            return False
        return True

    async def load_persistent_rules(
        self, *, directory: str,
    ) -> list["ApprovalRule"]:
        """Return an empty list — OpenCode durable rules arrive through
        the ``permission.asked.always`` event arm, not by reading any
        on-disk file.
        """
        del directory
        return []


def _opencode_permission_for_rule(
    rule: "ApprovalRule",
) -> tuple[str, str] | None:
    """Translate an ``ApprovalRule`` to an OpenCode ``(permission, pattern)``.

    The session-scoped rule shape in OpenShrimp ships a hooks-style
    ``ApprovalRule(tool_name="Bash", pattern="git *")``; OpenCode's
    permission keys are lowercase (``bash``).  Returns None for rules
    that don't have an OpenCode mapping (e.g. blanket non-Bash rules
    we don't want to durably persist through this path).
    """
    if rule.pattern is None:
        return None
    name = rule.tool_name
    if not name:
        return None
    # Hooks-style tool names are capitalised (``Bash``); OpenCode wire
    # names are lowercase (``bash``).  The session-rule registry stores
    # the hooks-style name; downstream OpenCode needs lowercase.
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            key = f"{parts[1]}_{parts[2]}"
        else:
            key = name
    elif name.startswith("_"):
        key = name
    else:
        key = name.lower()
    return key, rule.pattern


__all__ = [
    "HOST_BASH_TOOL_NAME",
    "OpenCodePolicy",
]
