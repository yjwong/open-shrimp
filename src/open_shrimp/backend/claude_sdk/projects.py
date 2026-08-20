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

from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path

from open_shrimp.backend.claude_sdk.mcp_config import load_claude_config
from open_shrimp.config import check_directory, unique_context_name


@dataclass(frozen=True)
class ClaudeProject:
    """One importable project: where it is, what to call it, and how recent.

    ``name`` is the folder as it reads on disk, which is what the user
    recognises.  ``context_name`` is what that folder must be called in
    ``config.yaml``, which is a narrower thing: a folder may be
    ``talenthub.glints.com`` and a context may not.  Both are carried
    because both are shown — the tick list names the folder, and the config
    names the context.
    """

    directory: str
    name: str
    context_name: str
    last_start_time: int | None


def name_directory(directory: str, taken: Collection[str]) -> ClaudeProject:
    """What one folder is called: on disk, and in ``config.yaml``.

    The only place a ``ClaudeProject`` is built from a bare path, so discovery,
    the ``projects name`` command and the terminal wizard's manual-path prompt
    cannot name the same folder differently.  *taken* is what the caller has
    already used, because uniqueness is a property of the list being built and
    only the caller knows it.

    The path goes through :func:`check_directory`, the one answer given
    wherever a context directory is checked, so a folder named by one spelling
    is named the same by any other: ``.``, ``~/work/api`` and a symlink to it
    are one folder and get one name.  A path that does not exist is still
    named — naming is not validation, and the config write that follows is
    where a missing directory is refused — from the path as typed, expanded
    but unresolved, so the answer echoes what the user can still correct.
    """
    path = check_directory(directory)["path"]
    label = Path(path).name or path
    return ClaudeProject(path, label, unique_context_name(label, taken), None)


def discover_claude_projects() -> list[ClaudeProject]:
    """Report the Claude Code projects worth offering as contexts.

    Newest session first.  An absent, unreadable or malformed
    ``~/.claude.json`` yields an empty list rather than an error: a fresh
    machine has no such file, and setup must still reach its next question.
    """
    projects = load_claude_config().get("projects")
    if not isinstance(projects, dict):
        return []

    found: list[tuple[str, int | None]] = []
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
            (directory, int(started) if isinstance(started, (int, float)) else None)
        )

    found.sort(key=lambda entry: entry[1] or 0, reverse=True)

    # Named after sorting, never before: the suffix a collision earns depends
    # on which folder is seen first, and the order the user is shown is the
    # order that should decide it.
    taken: set[str] = set()
    named: list[ClaudeProject] = []
    for directory, started in found:
        project = replace(name_directory(directory, taken), last_start_time=started)
        taken.add(project.context_name)
        named.append(project)
    return named


__all__ = ["ClaudeProject", "discover_claude_projects", "name_directory"]
