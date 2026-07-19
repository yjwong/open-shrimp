"""Tests for the on-disk task-checklist store reader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_shrimp.agent_status import current_todo_text, todo_counts
from open_shrimp.backend.claude_sdk.task_checklist import read_checklist


def _write_task(
    tasks_dir: Path, task_id: str, **fields: Any,
) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    data = {"id": task_id, **fields}
    (tasks_dir / f"{task_id}.json").write_text(json.dumps(data))


def test_reads_well_formed_tasks(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "sess-1"
    _write_task(
        tasks_dir, "1",
        subject="Fix the bug",
        status="completed",
        activeForm="Fixing the bug",
    )
    _write_task(
        tasks_dir, "2",
        subject="Run tests",
        status="in_progress",
        activeForm="Running tests",
    )
    todos = read_checklist(tmp_path, "sess-1")
    assert todos == [
        {
            "content": "Fix the bug",
            "status": "completed",
            "activeForm": "Fixing the bug",
        },
        {
            "content": "Run tests",
            "status": "in_progress",
            "activeForm": "Running tests",
        },
    ]


def test_numeric_id_ordering(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "s"
    for task_id in ("10", "2", "1"):
        _write_task(tasks_dir, task_id, subject=f"task {task_id}", status="pending")
    todos = read_checklist(tmp_path, "s")
    assert [t["content"] for t in todos] == ["task 1", "task 2", "task 10"]


def test_skips_dotfiles_and_malformed_json(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "s"
    _write_task(tasks_dir, "1", subject="good", status="pending")
    (tasks_dir / ".lock").write_text("")
    (tasks_dir / ".highwatermark").write_text("3")
    (tasks_dir / "2.json").write_text("{not json")
    (tasks_dir / "3.json").write_text('["not", "a", "dict"]')
    todos = read_checklist(tmp_path, "s")
    assert [t["content"] for t in todos] == ["good"]


def test_missing_session_dir_returns_empty(tmp_path: Path) -> None:
    assert read_checklist(tmp_path, "no-such-session") == []


def test_deleted_task_absent(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "s"
    _write_task(tasks_dir, "1", subject="kept", status="pending")
    _write_task(tasks_dir, "2", subject="removed", status="pending")
    # status: deleted removes the file from the store.
    (tasks_dir / "2.json").unlink()
    todos = read_checklist(tmp_path, "s")
    assert [t["content"] for t in todos] == ["kept"]


def test_active_form_falls_back_to_subject(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "s"
    _write_task(tasks_dir, "1", subject="Do the thing", status="in_progress")
    todos = read_checklist(tmp_path, "s")
    assert todos[0]["activeForm"] == "Do the thing"


def test_output_feeds_agent_status_helpers(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "s"
    _write_task(tasks_dir, "1", subject="a", status="completed", activeForm="a-ing")
    _write_task(tasks_dir, "2", subject="b", status="in_progress", activeForm="b-ing")
    _write_task(tasks_dir, "3", subject="c", status="pending", activeForm="c-ing")
    todos = read_checklist(tmp_path, "s")
    assert todo_counts(todos) == (1, 3)
    assert current_todo_text(todos) == "b-ing"
