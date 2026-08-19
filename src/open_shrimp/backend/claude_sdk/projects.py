"""The projects the user already opened in Claude Code.

The Claude CLI records every directory it has run in under the ``projects``
key of ``~/.claude.json``, keyed on the absolute path.  Most of those entries
are not projects: a directory gets a key as soon as anything there is read,
and the entry then holds ``mcpServers`` and nothing else.  On a developer
machine those stubs outnumber the real ones.

Two fields separate the two.  ``lastSessionId`` says a session actually ran
there, and ``hasTrustDialogAccepted`` says the user trusted the folder on
purpose.  Both must hold, because each alone admits a folder the user never
chose to work in.

This filter has one implementation.  Every front end reaches it through
``openshrimp projects discover --json`` rather than reading the file itself,
so the two GUI wizards cannot drift from the terminal one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_shrimp.backend.claude_sdk.mcp_config import load_claude_config


@dataclass(frozen=True)
class ClaudeProject:
    """One importable project: where it is, what to call it, and how recent."""

    directory: str
    name: str
    last_start_time: int | None


def discover_claude_projects() -> list[ClaudeProject]:
    """Report the Claude Code projects worth offering as contexts.

    Newest session first.  An absent, unreadable or malformed
    ``~/.claude.json`` yields an empty list rather than an error: a fresh
    machine has no such file, and setup must still reach its next question.
    """
    projects = load_claude_config().get("projects")
    if not isinstance(projects, dict):
        return []

    found: list[ClaudeProject] = []
    for directory, entry in projects.items():
        if not isinstance(directory, str) or not isinstance(entry, dict):
            continue
        if not entry.get("lastSessionId"):
            continue
        if entry.get("hasTrustDialogAccepted") is not True:
            continue
        # A trusted folder with a session but no lastStartTime is real; the
        # key is simply absent on older entries, so it cannot gate the entry
        # and must only decide where in the order it lands.
        started = entry.get("lastStartTime")
        found.append(
            ClaudeProject(
                directory=directory,
                name=Path(directory).name or directory,
                last_start_time=(
                    int(started) if isinstance(started, (int, float)) else None
                ),
            )
        )

    found.sort(key=lambda project: project.last_start_time or 0, reverse=True)
    return found


__all__ = ["ClaudeProject", "discover_claude_projects"]
