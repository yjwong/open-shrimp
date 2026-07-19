"""Tests for the claude_sdk policy's checklist-tool taxonomy and summaries."""

from __future__ import annotations

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy


class TestChecklistTaxonomy:
    def test_is_checklist_tool(self) -> None:
        p = ClaudeSdkPolicy()
        for name in ("TaskCreate", "TaskUpdate"):
            assert p.is_checklist_tool(name) is True
        # Read-only checklist tools cannot change the store, and
        # background-task management is not the session checklist.
        for name in ("TaskList", "TaskGet", "TaskStop", "TaskOutput"):
            assert p.is_checklist_tool(name) is False

    def test_no_input_snapshots(self) -> None:
        # The Task tools are incremental: no input carries the full list,
        # so the stream always pulls through the ChecklistReader.
        p = ClaudeSdkPolicy()
        assert p.checklist_snapshot("TaskCreate", {"subject": "x"}) is None
        assert p.checklist_snapshot("TaskUpdate", {"taskId": "1"}) is None

    def test_read_only_checklist_tools_suppressed(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.suppress_notification("TaskList") is True
        assert p.suppress_notification("TaskGet") is True
        # The mutating pair keeps its inline progress narration.
        assert p.suppress_notification("TaskCreate") is False
        assert p.suppress_notification("TaskUpdate") is False


class TestChecklistSummaries:
    def test_task_create_shows_subject(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize(
            "TaskCreate", {"subject": "Fix the bug", "description": "…"}, None,
        ) == "Fix the bug"

    def test_task_create_truncates_long_subject(self) -> None:
        p = ClaudeSdkPolicy()
        summary = p.summarize("TaskCreate", {"subject": "x" * 80}, None)
        assert summary == "x" * 60 + "..."

    def test_task_update_status(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize(
            "TaskUpdate", {"taskId": "3", "status": "completed"}, None,
        ) == "#3 → completed"

    def test_task_update_field_change(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize(
            "TaskUpdate", {"taskId": "3", "subject": "new title"}, None,
        ) == "#3 subject"

    def test_task_update_deps(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize(
            "TaskUpdate", {"taskId": "2", "addBlockedBy": ["1"]}, None,
        ) == "#2 deps"

    def test_task_get_and_list(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize("TaskGet", {"taskId": "5"}, None) == "#5"
        assert p.summarize("TaskList", {}, None) == "list"
