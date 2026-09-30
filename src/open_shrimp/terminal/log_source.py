"""Log source abstraction for the terminal mini app.

Provides a unified ``LogSource`` type that the terminal API endpoints
use to resolve and tail different kinds of output: background task
output files, container build logs, etc.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from open_shrimp.sandbox import SandboxManager
from open_shrimp.sandbox.manager import lookup_active_build
from open_shrimp.handlers.state import is_task_active

logger = logging.getLogger(__name__)

# Task ID pattern: alphanumeric, used by Claude CLI (e.g. "brf4e7jzw")
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# task_type values that indicate an agent transcript (JSONL format).
_AGENT_TASK_TYPES = {"local_agent", "remote_agent"}

def _claude_tmp_base() -> Path:
    """Base directory for Claude CLI tmp files.

    Mirrors the CLI's ``getClaudeTempDir``: ``$CLAUDE_CODE_TMPDIR`` wins;
    otherwise ``/tmp/claude-{uid}`` on Unix (per-user suffix because /tmp
    is shared) and ``{tmpdir}/claude`` on Windows (the tmp dir is already
    per-user there).  Symlinks resolved so paths compare equal on macOS.
    """
    override = os.environ.get("CLAUDE_CODE_TMPDIR")
    if sys.platform == "win32":
        base = Path(override) if override else Path(tempfile.gettempdir())
        name = "claude"
    else:
        base = Path(override) if override else Path("/tmp")
        name = f"claude-{os.getuid()}"
    return base.resolve() / name


# Base directory for Claude CLI tmp files
_CLAUDE_TMP_BASE = _claude_tmp_base()


@dataclass
class LogSource:
    """A resolved log source that the terminal API can tail or read."""

    path: Path
    is_active: Callable[[], bool]
    render: str = "raw"  # "raw" for plain text, "jsonl" for agent task rendering


# ---------------------------------------------------------------------------
# File discovery helpers (moved from api.py)
# ---------------------------------------------------------------------------


def transient_task_output_path(project: str, task_id: str) -> Path:
    """Path a non-CLI producer should write a task transcript to.

    Mirrors the layout :func:`_search_tmp_base` scans
    (``<base>/<project>/tasks/<task_id>.output``) so the Terminal Mini App
    discovers and tails it the same way it does real CLI task output.  This
    is the single source of truth for that convention — producers must not
    hardcode the path themselves.
    """
    return _CLAUDE_TMP_BASE / project / "tasks" / f"{task_id}.output"


def _is_file_or_symlink(path: Path) -> bool:
    """Return True if *path* is a regular file or a symlink (even broken)."""
    return path.is_file() or path.is_symlink()


def _search_tmp_base(base: Path, filename: str) -> Path | None:
    """Search a Claude CLI tmp base directory for a task output file.

    Looks for ``<base>/<project>/tasks/<filename>`` and
    ``<base>/<project>/<session>/tasks/<filename>``.

    Also matches broken symlinks (common for agent tasks in containers
    where the symlink target uses a container-internal path).
    """
    if not base.is_dir():
        return None

    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue

        candidate = project_dir / "tasks" / filename
        if _is_file_or_symlink(candidate):
            return candidate

        for sub in project_dir.iterdir():
            if not sub.is_dir():
                continue
            candidate = sub / "tasks" / filename
            if _is_file_or_symlink(candidate):
                return candidate

    return None


def _resolve_guest_symlink(
    symlink: Path, context_dir: Path,
) -> Path | None:
    """Resolve a broken symlink created inside a guest to its host path.

    The agent's ``.claude`` home is shared into the guest from the host.
    Agent task ``.output`` files are symlinks to ``.jsonl`` session files
    under ``<guest-home>/.claude/projects/…``, which don't exist on the host
    at that path.  This function translates the guest path back to the host
    equivalent: *context_dir* holds the agent home
    (``claude_sdk.runtime.claude_home_dir``), which is shared into the guest
    as ``<guest-home>/.claude``.

    The guest home varies by backend and user (``/home/openshrimp`` for
    libvirt and HCS, ``/home/<user>.guest`` for Lima), so the stable
    ``/.claude/`` marker is what the translation keys off rather than a
    hardcoded prefix.
    """
    try:
        target = os.readlink(symlink)
    except OSError:
        return None

    host_path = _guest_to_host(target, context_dir)
    if host_path is not None and host_path.is_file():
        return host_path
    return None


def _guest_to_host(guest_path: str, context_dir: Path) -> Path | None:
    """Map a path under a guest's ``~/.claude`` to the host directory shared
    as it, keyed off the ``/.claude/`` marker (see
    :func:`_resolve_guest_symlink`)."""
    from open_shrimp.backend.claude_sdk.runtime import claude_home_dir

    marker = "/.claude/"
    idx = guest_path.find(marker)
    if idx == -1:
        return None
    return claude_home_dir(context_dir) / guest_path[idx + len(marker):]


def _sandbox_context_dirs(
    sandbox_managers: dict[str, SandboxManager] | None,
) -> list[Path]:
    """Every sandboxed context's state directory across all managers."""
    dirs: list[Path] = []
    for mgr in (sandbox_managers or {}).values():
        if mgr.state_dir.is_dir():
            dirs.extend(p for p in mgr.state_dir.iterdir() if p.is_dir())
    return dirs


def _find_task_output_file(
    task_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> Path | None:
    """Find the output file for a background task by ID.

    Searches the host Claude CLI tmp directory and all sandbox managers'
    state directories (where sandboxed contexts write their tmp files).

    For a sandboxed agent task the ``.output`` file is a symlink whose target
    uses a guest-internal path.  When a broken symlink is found in a sandbox
    state directory, the target is translated to the host equivalent so the
    caller can read the actual data.
    """
    if not _TASK_ID_RE.match(task_id):
        return None

    filename = f"{task_id}.output"

    # Search the host tmp directory first.
    result = _search_tmp_base(_CLAUDE_TMP_BASE, filename)
    if result:
        return result

    # Search all sandbox managers' state directories.
    for context_dir in _sandbox_context_dirs(sandbox_managers):
        tmp_dir = context_dir / "tmp"
        result = _search_tmp_base(tmp_dir, filename)
        if result:
            # Broken symlink — resolve guest path to host path.
            if result.is_symlink() and not result.exists():
                resolved = _resolve_guest_symlink(
                    result, context_dir,
                )
                if resolved:
                    return resolved
            return result

    return None


def _is_agent_output(path: Path, task_type: str | None) -> bool:
    """Determine if a task output file is an agent JSONL transcript."""
    if task_type:
        return task_type in _AGENT_TASK_TYPES
    # Fallback: agent output files are symlinks to .jsonl files.
    try:
        return path.is_symlink() and os.readlink(path).endswith(".jsonl")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Workflow runs
# ---------------------------------------------------------------------------

# Agent ID pattern: the CLI's hex agent ids (e.g. "a29aed8856e110805").
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9]+$")


@dataclass
class WorkflowAgent:
    """One agent a workflow script spawned, as its run journal records it."""

    agent_id: str
    label: str
    phase: str
    #: ``running``; ``done`` once it returned a result; ``stopped`` when the
    #: run ended without one (the agent failed or the run was cancelled).
    state: str


def _claude_homes(
    sandbox_managers: dict[str, SandboxManager] | None,
) -> list[Path]:
    """The host's Claude config home, then every sandboxed context's."""
    from claude_agent_sdk._internal.sessions import _get_claude_config_home_dir

    from open_shrimp.backend.claude_sdk.runtime import claude_home_dir

    homes = [_get_claude_config_home_dir()]
    homes.extend(
        claude_home_dir(d) for d in _sandbox_context_dirs(sandbox_managers)
    )
    return homes


def _workflow_run_dir(
    task_id: str,
    sandbox_managers: dict[str, SandboxManager] | None,
) -> Path | None:
    """The host directory holding a workflow task's agent transcripts.

    The CLI reports the directory when it launches the run; that report is
    lost on a restart, so a finished run is also found from its ``.output``
    file, which sits at ``<tmp>/<project>/<session>/tasks/`` and lists its
    agents' ids, under ``projects/<project>/<session>/`` of a Claude home.
    """
    from open_shrimp.backend.claude_sdk import workflow

    reported = workflow.transcript_dir(task_id)
    if reported is not None:
        path = Path(reported)
        if path.is_dir():
            return path
        for context_dir in _sandbox_context_dirs(sandbox_managers):
            host_path = _guest_to_host(reported, context_dir)
            if host_path is not None and host_path.is_dir():
                return host_path

    output = _find_task_output_file(task_id, sandbox_managers=sandbox_managers)
    if output is None:
        return None
    try:
        snapshot = json.loads(output.read_text()).get("workflowProgress")
    except (OSError, ValueError, AttributeError):
        return None
    agent_ids = [
        entry["agentId"]
        for entry in snapshot or []
        if isinstance(entry, dict)
        and isinstance(entry.get("agentId"), str)
        and _AGENT_ID_RE.match(entry["agentId"])
    ]
    if not agent_ids:
        return None
    session_dir = output.parent.parent
    project, session = session_dir.parent.name, session_dir.name
    for home in _claude_homes(sandbox_managers):
        runs = home / "projects" / project / session / "subagents" / "workflows"
        for transcript in runs.glob(f"*/agent-{agent_ids[0]}.jsonl"):
            return transcript.parent
    return None


def _read_journal(run_dir: Path) -> list[dict]:
    try:
        lines = (run_dir / "journal.jsonl").read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            # The CLI may be mid-way through appending the last line.
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def list_workflow_agents(
    task_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> list[WorkflowAgent] | None:
    """The agents a workflow task has started so far, in start order.

    None when the run's transcripts cannot be found.  The journal records
    ``started`` when an agent begins and ``result`` when it returns, and
    nothing for an agent that fails, so an agent without a result is
    running until the task ends.
    """
    if not _TASK_ID_RE.match(task_id):
        return None
    run_dir = _workflow_run_dir(task_id, sandbox_managers)
    if run_dir is None:
        return None

    task_active = is_task_active(task_id)
    agents: dict[str, WorkflowAgent] = {}
    for entry in _read_journal(run_dir):
        agent_id = entry.get("agentId")
        if not isinstance(agent_id, str):
            continue
        if entry.get("type") == "started" and agent_id not in agents:
            agents[agent_id] = WorkflowAgent(
                agent_id=agent_id,
                label=str(entry.get("label") or agent_id),
                phase=str(entry.get("phase") or ""),
                state="running" if task_active else "stopped",
            )
        elif entry.get("type") == "result" and agent_id in agents:
            agents[agent_id].state = "done"
    return list(agents.values())


def resolve_workflow_agent(
    task_id: str,
    agent_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve one agent of a workflow task to its live transcript."""
    if not _TASK_ID_RE.match(task_id) or not _AGENT_ID_RE.match(agent_id):
        return None
    run_dir = _workflow_run_dir(task_id, sandbox_managers)
    if run_dir is None:
        return None
    path = run_dir / f"agent-{agent_id}.jsonl"
    if not path.is_file():
        return None

    def is_active() -> bool:
        if not is_task_active(task_id):
            return False
        return not any(
            entry.get("type") == "result" and entry.get("agentId") == agent_id
            for entry in _read_journal(run_dir)
        )

    return LogSource(path=path, is_active=is_active, render="jsonl")


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

# Context name pattern for build IDs.
_CONTEXT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def resolve_task(
    source_id: str,
    task_type: str | None = None,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve a background task ID to a ``LogSource``."""
    path = _find_task_output_file(source_id, sandbox_managers=sandbox_managers)
    if path is None:
        return None

    render = "jsonl" if _is_agent_output(path, task_type) else "raw"
    tid = source_id  # capture for closure

    return LogSource(
        path=path,
        is_active=lambda: is_task_active(tid),
        render=render,
    )


def resolve_container_build(
    source_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve a container build context name to a ``LogSource``.

    Uses the global build registry as the authoritative source for active
    builds.  This avoids the bug where multiple sandbox managers share the
    same ``build_log_dir`` and the wrong manager's ``is_build_active`` is
    captured.  Falls back to scanning managers for finished builds (log
    file exists but no active registration).
    """
    if not _CONTEXT_NAME_RE.match(source_id):
        return None

    # Primary: look up the global build registry (populated by
    # register_build, cleared by unregister_build).
    entry = lookup_active_build(source_id)
    if entry is not None:
        log_path, mgr = entry
        if log_path.is_file():
            ctx = source_id  # capture for closure
            _mgr = mgr  # capture for closure
            return LogSource(
                path=log_path,
                is_active=lambda: _mgr.is_build_active(ctx),
                render="raw",
            )

    # Fallback: build already finished — find the log file for reading.
    if sandbox_managers:
        for mgr in sandbox_managers.values():
            log_path = mgr.build_log_dir / f"{source_id}.log"
            if log_path.is_file():
                return LogSource(
                    path=log_path,
                    is_active=lambda: False,
                    render="raw",
                )

    return None


def resolve(
    source_type: str,
    source_id: str,
    task_type: str | None = None,
    sandbox_managers: dict[str, SandboxManager] | None = None,
    agent_id: str | None = None,
) -> LogSource | None:
    """Resolve a ``(type, id)`` pair to a ``LogSource``.

    Args:
        source_type: The type of log source (``"task"``,
            ``"workflow_agent"`` or ``"container_build"``).
        source_id: The identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        sandbox_managers: Managers dict for build log and state dirs.
        agent_id: The agent within the workflow task *source_id* (only
            for ``type=workflow_agent``).

    Returns:
        A ``LogSource`` or ``None`` if the source cannot be found.
    """
    if source_type == "task":
        return resolve_task(
            source_id, task_type=task_type, sandbox_managers=sandbox_managers,
        )
    elif source_type == "workflow_agent":
        if not agent_id:
            return None
        return resolve_workflow_agent(
            source_id, agent_id, sandbox_managers=sandbox_managers,
        )
    elif source_type == "container_build":
        return resolve_container_build(
            source_id, sandbox_managers=sandbox_managers,
        )
    else:
        logger.warning("Unknown log source type: %s", source_type)
        return None
