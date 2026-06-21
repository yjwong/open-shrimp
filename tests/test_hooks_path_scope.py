"""Tests for path-scoped tool approval and session-approved-dirs in hooks.py.

Covers the "Claude-Code-style" UX:
- Out-of-scope path-tool calls always prompt; blanket "Approve all <Tool>"
  rules cannot bypass the directory boundary.
- Paths within session-approved dirs auto-approve for any tool, including
  the mutating ones (Edit/Write).
- The suggested directory passed to ``request_approval`` matches Claude
  Code's parent-of-file granularity.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from open_shrimp.backend.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from open_shrimp.hooks import (
    ApprovalRule,
    _suggested_session_dir,
    make_can_use_tool,
    matches_approval_rule,
)


def _ctx(tool_use_id: str = "tu_1") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


# ---------------------------------------------------------------------------
# _suggested_session_dir
# ---------------------------------------------------------------------------


class TestSuggestedSessionDir:
    def test_read_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Read", {"file_path": "/etc/passwd"},
        ) == "/etc"

    def test_edit_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Edit", {"file_path": "/var/log/syslog"},
        ) == "/var/log"

    def test_write_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Write", {"file_path": "/tmp/out.txt"},
        ) == "/tmp"

    def test_glob_returns_path_itself(self) -> None:
        assert _suggested_session_dir("Glob", {"path": "/etc"}) == "/etc"

    def test_grep_returns_path_itself(self) -> None:
        assert _suggested_session_dir("Grep", {"path": "/var/log"}) == "/var/log"

    def test_glob_without_path_returns_none(self) -> None:
        assert _suggested_session_dir("Glob", {}) is None

    def test_read_without_file_path_returns_none(self) -> None:
        assert _suggested_session_dir("Read", {}) is None

    def test_non_path_tool_returns_none(self) -> None:
        assert _suggested_session_dir("Bash", {"command": "ls"}) is None
        assert _suggested_session_dir("WebFetch", {"url": "x"}) is None


# ---------------------------------------------------------------------------
# make_can_use_tool: in-scope vs out-of-scope path checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMakeCanUseToolPathScope:
    async def test_in_cwd_read_auto_approves(self, tmp_path: Path) -> None:
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
        )
        target = tmp_path / "f.txt"
        target.write_text("x")
        result = await can_use("Read", {"file_path": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_additional_dir_read_auto_approves(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"
        extra = tmp_path / "extra"
        cwd.mkdir(); extra.mkdir()
        target = extra / "f.txt"
        target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            additional_directories=[str(extra)],
        )
        result = await can_use("Read", {"file_path": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_out_of_scope_read_prompts(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        target = outside / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
        )
        result = await can_use("Read", {"file_path": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_awaited_once()
        # Suggested dir is the parent of the file (Claude Code parity).
        args = request_approval.await_args.args
        assert args[0] == "Read"
        assert args[2] == "tu_1"
        assert args[3] == str(outside)

    async def test_out_of_scope_glob_uses_path_as_suggested_dir(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
        )
        await can_use("Glob", {"path": str(outside), "pattern": "*"}, _ctx())
        request_approval.assert_awaited_once()
        assert request_approval.await_args.args[3] == str(outside)

    async def test_in_scope_does_not_pass_suggested_dir(
        self, tmp_path: Path,
    ) -> None:
        # When the path falls inside scope, Read auto-approves and never
        # reaches the prompt — so no suggested_dir is computed at all.
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
        )
        target = tmp_path / "f.txt"; target.write_text("x")
        await can_use("Read", {"file_path": str(target)}, _ctx())
        request_approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Blanket "Approve all <Tool>" rule does NOT bypass directory boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPathToolGateBlocksBlanketRule:
    async def test_approve_all_read_does_not_bypass_out_of_scope(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        target = outside / "secret.txt"; target.write_text("s")

        # User has previously clicked some hypothetical "Approve all Read" —
        # we still expect the prompt to fire for an out-of-scope path.
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            is_tool_auto_approved=lambda tn, ti: tn == "Read",
        )
        result = await can_use("Read", {"file_path": str(target)}, _ctx())
        assert isinstance(result, PermissionResultDeny)
        request_approval.assert_awaited_once()

    async def test_blanket_rule_still_works_for_in_scope_tool(
        self, tmp_path: Path,
    ) -> None:
        # WebFetch isn't path-scoped — the blanket rule should auto-approve
        # without ever prompting (this preserves existing behavior).
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_tool_auto_approved=lambda tn, ti: tn == "WebFetch",
        )
        result = await can_use("WebFetch", {"url": "https://x"}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Session-approved dirs grant full access (read AND write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionApprovedDirs:
    async def test_session_dir_auto_approves_read(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            get_session_approved_dirs=lambda: [str(approved)],
        )
        result = await can_use("Read", {"file_path": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_session_dir_auto_approves_edit_without_accept_all_edits(
        self, tmp_path: Path,
    ) -> None:
        # Edits in session-approved dirs bypass the accept-all-edits gate.
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        notify = AsyncMock()
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            is_edit_auto_approved=lambda: False,
            notify_auto_approved_edit=notify,
            get_session_approved_dirs=lambda: [str(approved)],
        )
        result = await can_use(
            "Edit",
            {"file_path": str(target), "old_string": "x", "new_string": "y"},
            _ctx(),
        )
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()
        # User still gets to see the diff.
        notify.assert_awaited_once()

    async def test_in_cwd_edit_still_requires_accept_all_edits(
        self, tmp_path: Path,
    ) -> None:
        # Within static cwd (not session-approved), Edit still prompts when
        # accept-all-edits is off — session-dirs don't change cwd semantics.
        target = tmp_path / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_edit_auto_approved=lambda: False,
        )
        await can_use(
            "Edit",
            {"file_path": str(target), "old_string": "x", "new_string": "y"},
            _ctx(),
        )
        request_approval.assert_awaited_once()

    async def test_session_dirs_recomputed_per_call(
        self, tmp_path: Path,
    ) -> None:
        # The callback is consulted fresh on each invocation so newly-added
        # session dirs take effect immediately for the next tool call.
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        dirs: list[str] = []
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            get_session_approved_dirs=lambda: list(dirs),
        )
        # First call: dirs empty -> prompt.
        result1 = await can_use("Read", {"file_path": str(target)}, _ctx("a"))
        assert isinstance(result1, PermissionResultDeny)
        # User clicks the dir button — simulate by mutating dirs.
        dirs.append(str(approved))
        # Second call: now in scope -> auto-approve.
        result2 = await can_use("Read", {"file_path": str(target)}, _ctx("b"))
        assert isinstance(result2, PermissionResultAllow)


# ---------------------------------------------------------------------------
# Existing behavior preserved: matches_approval_rule sanity
# ---------------------------------------------------------------------------


def test_matches_approval_rule_blanket() -> None:
    rule = ApprovalRule(tool_name="WebFetch", pattern=None)
    assert matches_approval_rule(rule, "WebFetch", {"url": "x"}) is True
    assert matches_approval_rule(rule, "WebSearch", {"query": "x"}) is False


# --- NotebookEdit ---


from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy


def test_notebook_edit_is_path_scoped() -> None:
    assert ClaudeSdkPolicy().is_path_scoped("NotebookEdit") is True


def test_notebook_edit_is_mutating() -> None:
    assert ClaudeSdkPolicy().is_mutating("NotebookEdit") is True


def test_notebook_edit_is_file_targeted() -> None:
    assert ClaudeSdkPolicy().is_file_targeted("NotebookEdit") is True


def test_notebook_edit_suppress_notification() -> None:
    assert ClaudeSdkPolicy().suppress_notification("NotebookEdit") is True


def test_notebook_edit_extract_path() -> None:
    p = ClaudeSdkPolicy()
    assert p.extract_path(
        "NotebookEdit", {"notebook_path": "/x/y.ipynb"}, "/cwd",
    ) == "/x/y.ipynb"
    assert p.extract_path("NotebookEdit", {}, "/cwd") is None


def test_notebook_edit_suggested_session_dir() -> None:
    assert ClaudeSdkPolicy().suggested_session_dir(
        "NotebookEdit", {"notebook_path": "/var/data/x.ipynb"},
    ) == "/var/data"


def test_notebook_edit_summarize() -> None:
    assert ClaudeSdkPolicy().summarize(
        "NotebookEdit", {"notebook_path": "/cwd/x.ipynb"}, "/cwd",
    ) == "x.ipynb"


def test_notebook_edit_format_approval_text() -> None:
    text = ClaudeSdkPolicy().format_approval_text(
        "NotebookEdit",
        {"notebook_path": "/cwd/x.ipynb",
         "old_string": "a", "new_string": "b"},
        "/cwd",
    )
    assert "NotebookEdit" in text
    # MarkdownV2 escapes the dot: ``x.ipynb`` -> ``x\.ipynb``.
    assert "x\\.ipynb" in text


@pytest.mark.asyncio
async def test_notebook_edit_in_cwd_requires_approval(
    tmp_path: Path,
) -> None:
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval, cwd=str(tmp_path),
    )
    target = tmp_path / "nb.ipynb"
    target.write_text("{}")
    await can_use(
        "NotebookEdit",
        {"notebook_path": str(target),
         "old_string": "a", "new_string": "b"},
        _ctx("t_nb_1"),
    )
    request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_notebook_edit_accept_all_edits_auto_approves(
    tmp_path: Path,
) -> None:
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        is_edit_auto_approved=lambda: True,
    )
    target = tmp_path / "nb.ipynb"
    target.write_text("{}")
    result = await can_use(
        "NotebookEdit",
        {"notebook_path": str(target),
         "old_string": "a", "new_string": "b"},
        _ctx("t_nb_2"),
    )
    assert isinstance(result, PermissionResultAllow)
    request_approval.assert_not_awaited()


# --- auto_approved_at_session_start seeding ---


def test_seed_includes_task_management() -> None:
    names = set(ClaudeSdkPolicy().auto_approved_at_session_start())
    expected = {
        "TaskCreate", "TaskGet", "TaskUpdate",
        "TaskList", "TaskStop", "TaskOutput",
    }
    assert expected <= names


def test_seed_includes_mode_transitions() -> None:
    names = set(ClaudeSdkPolicy().auto_approved_at_session_start())
    assert {"EnterPlanMode", "EnterWorktree", "ExitWorktree"} <= names


def test_seed_includes_registry_discovery() -> None:
    names = set(ClaudeSdkPolicy().auto_approved_at_session_start())
    assert {"ToolSearch", "ListMcpResources", "ReadMcpResource"} <= names


def test_seed_excludes_exit_plan_mode() -> None:
    names = set(ClaudeSdkPolicy().auto_approved_at_session_start())
    assert "ExitPlanMode" not in names


def test_seed_excludes_user_facing_tools() -> None:
    names = set(ClaudeSdkPolicy().auto_approved_at_session_start())
    for tool in ("Edit", "Write", "Bash", "Read", "AskUserQuestion"):
        assert tool not in names
