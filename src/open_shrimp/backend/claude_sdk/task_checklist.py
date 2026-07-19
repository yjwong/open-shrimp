"""Read the CLI's on-disk task checklist store.

The Claude CLI persists the session checklist (TaskCreate/TaskUpdate/
TaskList/TaskGet) as one JSON file per task under
``<claude-home>/tasks/<session-id>/<taskId>.json``.  That directory is
host-visible in every deployment shape — the host's ``~/.claude`` directly,
or the per-context claude-home that sandboxes mount as the guest's
``~/.claude`` — so the checklist source of truth is a stateless directory
read: no stream reducer, no hook plumbing.

The reader returns ``{"content", "status", "activeForm"}`` dicts — the shape
every downstream consumer (the pinned-message renderer,
``agent_status.todo_counts``/``current_todo_text``) expects.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _id_sort_key(task_id: str) -> tuple[int, int, str]:
    """Numeric ids in numeric order (``#2`` < ``#10``), then the rest
    lexicographically."""
    if task_id.isdigit():
        return (0, int(task_id), "")
    return (1, 0, task_id)


def read_checklist(claude_home: Path, session_id: str) -> list[dict[str, Any]]:
    """Read the task checklist for *session_id* from the store.

    Scans ``<claude_home>/tasks/<session_id>/*.json``, skipping dotfiles
    (``.lock``, ``.highwatermark``) and malformed JSON, sorts by numeric
    task id, and returns ``{"content", "status", "activeForm"}`` dicts.
    A missing session directory (no tasks created yet) returns ``[]``.
    """
    tasks_dir = claude_home / "tasks" / session_id
    entries: list[tuple[str, dict[str, Any]]] = []
    try:
        children = list(tasks_dir.iterdir())
    except OSError:
        return []
    for path in children:
        if path.name.startswith(".") or path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("Skipping unreadable task file %s", path, exc_info=True)
            continue
        if not isinstance(data, dict):
            continue
        entries.append((str(data.get("id") or path.stem), data))
    entries.sort(key=lambda e: _id_sort_key(e[0]))
    return [
        {
            "content": data.get("subject", ""),
            "status": data.get("status", "pending"),
            "activeForm": data.get("activeForm") or data.get("subject", ""),
        }
        for _, data in entries
    ]
