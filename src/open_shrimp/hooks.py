"""Tool permission callbacks for OpenShrimp.

Implements the ``canUseTool`` callback the backend invokes when a tool is
not auto-approved.  We present a Telegram inline keyboard and await the
user's decision.

User-question tools (e.g. AskUserQuestion) are handled specially: the
hook presents questions to the user via Telegram, collects answers, then
denies the tool (to prevent the CLI from trying its own interactive UI)
while passing the answers back via the deny message so the model
receives them.

Path-scoped auto-approval: read-only file-access tools (Read, Glob,
Grep) are auto-approved when their target paths resolve to within the
context's working directory.  Mutating tools (Edit, Write, NotebookEdit)
always require explicit approval, even within the working directory,
unless the user has opted into "accept all edits" for the current
session.  In containerized contexts, all path-scoped tools (including
Edit/Write) are auto-approved regardless of path, since the sandbox
provides the safety boundary.  Paths outside the working directory
always fall through to the interactive Telegram approval prompt.

The per-tool taxonomy lives in each backend's ``policy.py`` module; this
file is the orchestration that consumes it via the ``BackendPolicy``
protocol.
"""

import fnmatch
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from open_shrimp.backend.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

if TYPE_CHECKING:
    from open_shrimp.backend.policy import BackendPolicy

logger = logging.getLogger(__name__)

# Dedicated temp directory for file uploads.  Read access to files within
# this directory is auto-approved so the agent doesn't need extra
# permission to read user-uploaded attachments.
ATTACHMENT_TEMP_DIR = Path(tempfile.gettempdir()) / "openshrimp_uploads"

# Type for the approval callback: receives tool_name, tool_input dict,
# tool_use_id, and an optional ``suggested_session_dir`` (set when the
# tool's target path is outside the approved directories — the caller
# may offer a "Allow <dir>/ this session" button); returns True (allow)
# or False (deny).
ApprovalCallback = Callable[
    [str, dict[str, Any], str, str | None], Awaitable[bool]
]

# Type for the question callback: receives list of question dicts,
# returns answers dict mapping question text -> answer string.
QuestionCallback = Callable[[list[dict[str, Any]]], Awaitable[dict[str, str]]]

# Outcome of a host_bash approval prompt.
HostBashOutcome = Literal["approved", "denied", "timeout"]

# Type for the host_bash approval callback: receives the host-escape tool's
# input dict and a tool_use_id, returns the resolution outcome.
HostBashApprovalCallback = Callable[
    [dict[str, Any], str], Awaitable[HostBashOutcome]
]

# Type for the auto-approved edit notification callback.
EditNotifyCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

# Type for the per-tool auto-approval check.
ToolAutoApprovedCallback = Callable[[str, dict[str, Any]], bool]


# ---------------------------------------------------------------------------
# Pattern-based approval rules
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRule:
    """A session-scoped auto-approval rule.

    ``tool_name`` must always match.  When ``pattern`` is set, it is matched
    against the tool's input using fnmatch glob semantics (for Bash this is
    the command string).  A ``None`` pattern means blanket approval for the
    tool.
    """

    tool_name: str
    pattern: str | None = None


def matches_approval_rule(
    rule: ApprovalRule,
    tool_name: str,
    tool_input: dict[str, Any],
    policy: "BackendPolicy | None" = None,
) -> bool:
    """Return True if *rule* matches the given tool invocation.

    For Bash-like pattern rules (e.g. ``git *``), compound commands are
    **not** matched — prefix/wildcard allow rules skip compound
    commands to prevent ``git *`` from auto-approving
    ``git status && rm -rf /``.  Blanket rules (``pattern is None``)
    still match compound commands.

    ``policy`` decides which tools get bash-pattern semantics (SDK:
    ``Bash``; OpenCode: ``bash``).  Resolved lazily when omitted.
    """
    if rule.tool_name != tool_name:
        return False
    if rule.pattern is None:
        return True
    # For Bash-like tools, match the pattern against the full command
    # string but skip compound commands for safety.
    if _resolve_policy(policy).is_bash_like(tool_name):
        command = tool_input.get("command", "")
        from open_shrimp.bash_parse import is_compound_command
        if is_compound_command(command):
            return False
        return fnmatch.fnmatch(command, rule.pattern)
    return True


# ---------------------------------------------------------------------------
# Path checks — backend-agnostic
# ---------------------------------------------------------------------------


def _is_path_within_directory(path: str, directory: str) -> bool:
    """Check if a resolved path is within the given directory.

    Uses os.path.realpath to resolve symlinks and normalise, then checks
    that the path starts with the directory prefix (with a trailing separator
    to avoid prefix false positives like /home/user2 matching /home/user).
    Also allows an exact match (e.g. Glob on the cwd itself).
    """
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def _is_path_within_any_directory(
    path: str, directories: list[str]
) -> bool:
    """Check if a resolved path is within any of the given directories."""
    return any(_is_path_within_directory(path, d) for d in directories)


def tool_path_within_dir(
    tool_name: str,
    tool_input: dict[str, Any],
    directory: str,
    policy: "BackendPolicy | None" = None,
) -> bool:
    """Public: True if *tool_name*'s target path resolves inside *directory*.

    Used by the approval handlers to auto-resolve pending tool calls
    after a session-wide directory approval.  Returns False for tools
    that aren't path-scoped or that have no resolvable path.

    The ``policy`` argument is required in normal use — if omitted, the
    process-wide default backend's policy is used.  Kept optional only
    so existing test sites can resolve it implicitly.
    """
    p = _resolve_policy(policy)
    path = p.extract_path(tool_name, tool_input, directory)
    return path is not None and _is_path_within_directory(path, directory)


def _resolve_policy(policy: "BackendPolicy | None") -> "BackendPolicy":
    if policy is not None:
        return policy
    from open_shrimp.client_manager import resolve_backend

    return resolve_backend(None).policy


# ---------------------------------------------------------------------------
# Public test helpers — kept stable so existing tests keep passing
# ---------------------------------------------------------------------------


def _suggested_session_dir(
    tool_name: str, tool_input: dict[str, Any],
    policy: "BackendPolicy | None" = None,
) -> str | None:
    """Return the directory to suggest for "Allow <dir>/ this session".

    Test-facing helper that defers to the policy.  When ``policy`` is
    omitted, the process-wide default backend's policy is used.
    """
    return _resolve_policy(policy).suggested_session_dir(tool_name, tool_input)


# ---------------------------------------------------------------------------
# make_can_use_tool — the orchestration body
# ---------------------------------------------------------------------------


def make_can_use_tool(
    request_approval: ApprovalCallback,
    cwd: str,
    additional_directories: list[str] | None = None,
    handle_user_questions: QuestionCallback | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
    notify_auto_approved_edit: EditNotifyCallback | None = None,
    chat_id: int | None = None,
    is_tool_auto_approved: ToolAutoApprovedCallback | None = None,
    is_containerized: bool = False,
    get_session_approved_dirs: Callable[[], list[str]] | None = None,
    request_host_bash_approval: HostBashApprovalCallback | None = None,
    policy: "BackendPolicy | None" = None,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]:
    """Create a ``can_use_tool`` callback for the active backend.

    Tools the backend already auto-approves are handled inside the
    backend and never reach this callback.  This handles everything
    else, dispatching every per-tool decision through the supplied
    ``BackendPolicy``.

    Args:
        request_approval: Async callback that presents the tool call to
            the user and returns True to allow or False to deny.
        cwd: The context working directory for path-scoped auto-approval.
        additional_directories: Optional list of extra directories that
            are also approved for path-scoped auto-approval.
        handle_user_questions: Optional async callback for the backend's
            user-question tool (SDK: AskUserQuestion).
        is_edit_auto_approved: Optional callback that returns True if
            the user has opted into "accept all edits" for the current
            session.
        notify_auto_approved_edit: Optional async callback called when a
            mutating tool is auto-approved (accept-all-edits mode).
        chat_id: Optional Telegram chat ID.  When provided, the per-chat
            upload directory is added to the approved directories.
        is_tool_auto_approved: Optional callback that returns True if
            the user has opted into auto-approval for that specific
            tool (possibly with a pattern constraint) in the current
            session.
        is_containerized: When True, all sandbox-eligible tools (per the
            policy's ``container_auto_approve``) are auto-approved.
        get_session_approved_dirs: Optional callback returning the list
            of directories the user opted into via the "Allow <dir>/
            this session" button.
        request_host_bash_approval: Optional callback for the host_bash
            tool's dedicated approval flow.
        policy: The backend's tool taxonomy and rendering.  Required in
            normal use; when omitted, the process-wide default backend's
            policy is resolved lazily.
    """
    p = _resolve_policy(policy)

    static_approved_dirs = [cwd] + (additional_directories or [])
    if chat_id is not None:
        upload_dir = str(ATTACHMENT_TEMP_DIR / str(chat_id))
        static_approved_dirs.append(upload_dir)

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        # host_bash (sudo mode): always route to the dedicated approval
        # callback.  Never auto-approved by patterns, session rules, or
        # the containerized fast-path — the whole point is that this
        # tool escapes the sandbox, so every invocation gets a fresh
        # Telegram prompt with a 10-second auto-deny timer.
        if p.is_host_bash(tool_name):
            if request_host_bash_approval is None:
                logger.warning(
                    "host_bash invoked but no approval callback wired; denying"
                )
                return PermissionResultDeny(
                    message="host_bash approval is not configured.",
                )
            outcome = await request_host_bash_approval(
                tool_input, context.tool_use_id,
            )
            if outcome == "approved":
                return PermissionResultAllow()
            if outcome == "timeout":
                return PermissionResultDeny(
                    message=(
                        "Auto-denied: the user did not respond to the "
                        "host_bash approval prompt within 10 seconds. "
                        "They may be away — try again later, or fall "
                        "back to the sandboxed Bash tool."
                    ),
                )
            return PermissionResultDeny(
                message="User denied the host_bash command.",
            )

        # port_forward: list/remove don't expose new attack surface — only
        # create needs the approval prompt.
        if tool_name == "mcp__openshrimp__port_forward":
            if tool_input.get("action") in ("list", "remove"):
                return PermissionResultAllow()

        # Special handling for user-question tools: present questions to
        # the user via Telegram, collect answers, then DENY the tool to
        # prevent the CLI from trying its own interactive UI.  The
        # answers are passed back to the model via the deny message.
        if p.handles_user_questions(tool_name) and handle_user_questions:
            questions = tool_input.get("questions", [])
            logger.info(
                "User-question tool %s with %d question(s)",
                tool_name, len(questions),
            )
            answers = await handle_user_questions(questions)
            logger.info("Collected answers: %s", answers)

            answer_lines = []
            for question_text, answer in answers.items():
                answer_lines.append(f"Q: {question_text}\nA: {answer}")
            answers_text = "\n\n".join(answer_lines)

            return PermissionResultDeny(
                message=(
                    "The user has already answered these questions via the "
                    "Telegram interface. Do not retry this tool call. "
                    "Here are their responses:\n\n" + answers_text
                ),
            )

        # Recompute approved directories on every call so newly-added
        # session-approved dirs take effect immediately.
        session_dirs: list[str] = (
            list(get_session_approved_dirs())
            if get_session_approved_dirs is not None
            else []
        )
        approved_dirs = static_approved_dirs + session_dirs

        # Containerized contexts: auto-approve all path-scoped tools
        # regardless of path, since the sandbox provides the safety
        # boundary.
        if is_containerized and p.is_path_scoped(tool_name):
            if p.is_mutating(tool_name) and notify_auto_approved_edit:
                try:
                    await notify_auto_approved_edit(tool_name, tool_input)
                except Exception:
                    logger.exception(
                        "Failed to send auto-approved edit notification"
                    )
            logger.info(
                "Auto-approved %s in containerized context", tool_name
            )
            return PermissionResultAllow()

        # Path-scoped approval for file-access tools.
        tool_path = p.extract_path(tool_name, tool_input, cwd)
        path_scoped_out_of_scope = False
        if tool_path is not None:
            if _is_path_within_any_directory(tool_path, session_dirs):
                if p.is_mutating(tool_name) and notify_auto_approved_edit:
                    try:
                        await notify_auto_approved_edit(tool_name, tool_input)
                    except Exception:
                        logger.exception(
                            "Failed to send auto-approved edit notification"
                        )
                logger.info(
                    "Auto-approved %s: path %s is within a session-approved dir",
                    tool_name,
                    tool_path,
                )
                return PermissionResultAllow()
            if _is_path_within_any_directory(tool_path, static_approved_dirs):
                if p.is_mutating(tool_name):
                    if is_edit_auto_approved and is_edit_auto_approved():
                        logger.info(
                            "Auto-approved %s (accept-all-edits): "
                            "path %s is within approved dirs",
                            tool_name,
                            tool_path,
                        )
                        if notify_auto_approved_edit:
                            try:
                                await notify_auto_approved_edit(
                                    tool_name, tool_input,
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to send auto-approved edit "
                                    "notification"
                                )
                        return PermissionResultAllow()
                    logger.info(
                        "Mutating tool %s within approved dirs requires "
                        "approval",
                        tool_name,
                    )
                else:
                    logger.info(
                        "Auto-approved %s: path %s is within approved dirs",
                        tool_name,
                        tool_path,
                    )
                    return PermissionResultAllow()
            else:
                path_scoped_out_of_scope = True
                logger.warning(
                    "Path-scoped tool %s targets %s outside approved dirs, "
                    "requiring manual approval",
                    tool_name,
                    tool_path,
                )

        # Accept-all-edits mode: also auto-approve common safe Bash
        # commands (mkdir, touch, rm, mv, cp, sed, etc.) that complement
        # file editing.  Routed through ``is_bash_like`` so the same
        # branch covers the SDK's ``Bash`` and OpenCode's ``bash`` —
        # ``is_safe_for_accept_edits_bash`` returns False on OpenCode
        # which has no equivalent allowlist semantic.
        if (
            p.is_bash_like(tool_name)
            and is_edit_auto_approved
            and is_edit_auto_approved()
            and p.is_safe_for_accept_edits_bash(
                tool_input.get("command", ""), approved_dirs,
            )
        ):
            logger.info(
                "Auto-approved safe Bash command (accept-all-edits): %s",
                tool_input.get("command", "")[:100],
            )
            return PermissionResultAllow()

        # Containerized contexts: auto-approve sandbox-eligible tools
        # (per the policy's container_auto_approve set — SDK: Bash +
        # Monitor; OpenCode: bash).
        if is_containerized and p.container_auto_approve(tool_name):
            logger.info(
                "Auto-approved %s in containerized context", tool_name
            )
            return PermissionResultAllow()

        # Multi-file mutating tools (OpenCode: apply_patch) carry their
        # own envelope and don't fit ``_PATH_SCOPED_TOOLS``.  Same
        # boundary rules as Edit/Write: containerized auto-approve,
        # accept-all-edits auto-approve when every targeted path
        # resolves inside ``static_approved_dirs``.
        if p.multi_file_mutating(tool_name):
            if is_containerized:
                if notify_auto_approved_edit:
                    try:
                        await notify_auto_approved_edit(tool_name, tool_input)
                    except Exception:
                        logger.exception(
                            "Failed to send auto-approved edit notification"
                        )
                logger.info(
                    "Auto-approved %s in containerized context", tool_name,
                )
                return PermissionResultAllow()
            if (
                is_edit_auto_approved
                and is_edit_auto_approved()
                and p.multi_file_paths_within(
                    tool_name, tool_input, cwd, static_approved_dirs,
                )
            ):
                if notify_auto_approved_edit:
                    try:
                        await notify_auto_approved_edit(tool_name, tool_input)
                    except Exception:
                        logger.exception(
                            "Failed to send auto-approved edit notification"
                        )
                logger.info(
                    "Auto-approved %s (accept-all-edits): all paths within "
                    "approved dirs", tool_name,
                )
                return PermissionResultAllow()

        # Per-tool session-scoped auto-approval (e.g. "Accept all git").
        # Skipped for path-scoped tools whose target was out-of-scope.
        if (
            not path_scoped_out_of_scope
            and is_tool_auto_approved
            and is_tool_auto_approved(tool_name, tool_input)
        ):
            logger.info(
                "Auto-approved %s (per-tool session approval)", tool_name
            )
            return PermissionResultAllow()

        logger.info("Requesting approval for tool: %s", tool_name)
        tool_use_id = context.tool_use_id
        suggested_dir = (
            p.suggested_session_dir(tool_name, tool_input)
            if path_scoped_out_of_scope
            else None
        )
        approved = await request_approval(
            tool_name, tool_input, tool_use_id, suggested_dir,
        )
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s", tool_name, decision)

        if approved:
            return PermissionResultAllow()
        else:
            return PermissionResultDeny(message="User denied tool use.")

    return can_use_tool
