"""Tests for the OpenCode ``BackendPolicy`` implementation.

Covers:

* Categorical queries return the right answers for the native vocabulary
  (``bash``, ``read``, ``edit``, ``write``, ``glob``, ``grep``,
  ``apply_patch``, ``todowrite``, ``agent``).
* ``extract_path`` reads ``filePath`` / ``path`` from the OpenCode input
  shape — the load-bearing fix that PR 2 unblocks.
* Path-scoped auto-approval works end-to-end on an OpenCode ``edit``
  event within cwd.
* ``apply_patch`` is treated as multi-file mutating, and
  ``multi_file_paths_within`` correctly checks every targeted path.
* ``handles_user_questions`` returns False for ``question`` — the
  blocking lifecycle stays on the SSE arm.
* The session-start auto-approve list is empty (OpenCode uses session-
  scoped category rules instead).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.opencode.policy import OpenCodePolicy
from open_shrimp.backend.types import (
    PermissionResultAllow,
    ToolPermissionContext,
)
from open_shrimp.hooks import make_can_use_tool


def _ctx(tool_use_id: str = "tu_1") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


# ---------------------------------------------------------------------------
# Categorical queries
# ---------------------------------------------------------------------------


class TestCategoricalQueries:
    def test_native_names_are_lowercase(self) -> None:
        p = OpenCodePolicy()
        assert p.is_path_scoped("read") is True
        assert p.is_path_scoped("edit") is True
        assert p.is_path_scoped("write") is True
        assert p.is_path_scoped("glob") is True
        assert p.is_path_scoped("grep") is True
        # SDK casing is NOT understood by the OpenCode policy.
        assert p.is_path_scoped("Edit") is False
        assert p.is_path_scoped("Read") is False

    def test_mutating_excludes_read_glob_grep(self) -> None:
        p = OpenCodePolicy()
        assert p.is_mutating("edit") is True
        assert p.is_mutating("write") is True
        assert p.is_mutating("read") is False
        assert p.is_mutating("glob") is False
        assert p.is_mutating("grep") is False

    def test_apply_patch_is_mutating_and_multi_file(self) -> None:
        p = OpenCodePolicy()
        assert p.is_mutating("apply_patch") is True
        assert p.multi_file_mutating("apply_patch") is True
        # apply_patch is NOT path-scoped — it carries its own envelope.
        assert p.is_path_scoped("apply_patch") is False

    def test_file_targeted_excludes_glob_grep(self) -> None:
        p = OpenCodePolicy()
        assert p.is_file_targeted("read") is True
        assert p.is_file_targeted("edit") is True
        assert p.is_file_targeted("write") is True
        assert p.is_file_targeted("glob") is False
        assert p.is_file_targeted("grep") is False

    def test_suppress_notification(self) -> None:
        p = OpenCodePolicy()
        assert p.suppress_notification("bash") is True
        assert p.suppress_notification("edit") is True
        assert p.suppress_notification("write") is True
        assert p.suppress_notification("apply_patch") is True
        assert p.suppress_notification("read") is False

    def test_container_auto_approve_only_bash(self) -> None:
        p = OpenCodePolicy()
        assert p.container_auto_approve("bash") is True
        # Monitor doesn't exist on OpenCode.
        assert p.container_auto_approve("Monitor") is False
        assert p.container_auto_approve("monitor") is False

    def test_is_bash_like(self) -> None:
        p = OpenCodePolicy()
        assert p.is_bash_like("bash") is True
        assert p.is_bash_like("openshrimp_host_bash") is True
        # SDK's Bash is not recognised.
        assert p.is_bash_like("Bash") is False

    def test_is_checklist_tool(self) -> None:
        p = OpenCodePolicy()
        assert p.is_checklist_tool("todowrite") is True
        # The SDK's Task tools are not OpenCode's wire vocabulary.
        assert p.is_checklist_tool("TaskUpdate") is False

    def test_checklist_snapshot_from_input(self) -> None:
        # todowrite carries the full list in its input.
        p = OpenCodePolicy()
        todos = [{"content": "x", "status": "pending"}]
        assert p.checklist_snapshot("todowrite", {"todos": todos}) == todos
        assert p.checklist_snapshot("todowrite", {}) == []
        assert p.checklist_snapshot("bash", {"todos": todos}) is None

    def test_is_host_bash(self) -> None:
        p = OpenCodePolicy()
        assert p.is_host_bash("openshrimp_host_bash") is True
        # SDK's MCP-prefixed name is not OpenCode's wire name.
        assert p.is_host_bash("mcp__openshrimp__host_bash") is False


# ---------------------------------------------------------------------------
# extract_path — reads filePath / path from the OpenCode input shape
# ---------------------------------------------------------------------------


class TestExtractPath:
    def test_read_extracts_filePath(self) -> None:
        p = OpenCodePolicy()
        # The load-bearing fix: OpenCode uses camelCase keys.
        assert p.extract_path(
            "read", {"filePath": "/tmp/x"}, "/cwd",
        ) == "/tmp/x"
        # SDK's snake_case key is ignored.
        assert p.extract_path(
            "read", {"file_path": "/tmp/x"}, "/cwd",
        ) is None

    def test_edit_extracts_filePath(self) -> None:
        p = OpenCodePolicy()
        assert p.extract_path(
            "edit", {"filePath": "/tmp/x"}, "/cwd",
        ) == "/tmp/x"

    def test_write_extracts_filePath(self) -> None:
        p = OpenCodePolicy()
        assert p.extract_path(
            "write", {"filePath": "/tmp/x"}, "/cwd",
        ) == "/tmp/x"

    def test_glob_defaults_to_cwd(self) -> None:
        p = OpenCodePolicy()
        assert p.extract_path("glob", {}, "/cwd") == "/cwd"

    def test_grep_path_when_provided(self) -> None:
        p = OpenCodePolicy()
        assert p.extract_path(
            "grep", {"path": "/etc"}, "/cwd",
        ) == "/etc"

    def test_apply_patch_returns_none(self) -> None:
        # apply_patch is NOT in the path-scoped map — it has its own
        # multi-file envelope path.
        p = OpenCodePolicy()
        assert p.extract_path(
            "apply_patch", {"patchText": "..."}, "/cwd",
        ) is None


class TestSuggestedSessionDir:
    def test_read_returns_parent_of_file(self) -> None:
        p = OpenCodePolicy()
        assert p.suggested_session_dir(
            "read", {"filePath": "/etc/passwd"},
        ) == "/etc"

    def test_glob_returns_directory_itself(self) -> None:
        p = OpenCodePolicy()
        assert p.suggested_session_dir(
            "glob", {"path": "/etc"},
        ) == "/etc"


# ---------------------------------------------------------------------------
# Auto-approval flow — end-to-end through can_use_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_within_cwd_requires_approval(tmp_path: Path) -> None:
    """An OpenCode ``edit`` event inside cwd should reach manual approval
    (mutating), not auto-approve."""
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        policy=OpenCodePolicy(),
    )
    target = tmp_path / "f.py"
    target.write_text("x")
    await can_use(
        "edit",
        {"filePath": str(target), "oldString": "x", "newString": "y"},
        _ctx("tu_edit"),
    )
    request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_within_cwd_auto_approves(tmp_path: Path) -> None:
    """Read-only ``read`` within cwd should auto-approve.

    This is the bug PR 2 fixes: pre-policy, the SDK-shape
    ``_PATH_SCOPED_TOOLS`` lookup ran on OpenCode's ``read`` event with
    ``filePath`` in the input but the SDK side looked for ``file_path``
    — the path was never extracted, so the auto-approval silently
    failed and every Read prompted the user.
    """
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        policy=OpenCodePolicy(),
    )
    target = tmp_path / "f.py"
    target.write_text("x")
    result = await can_use(
        "read", {"filePath": str(target)}, _ctx("tu_read"),
    )
    assert isinstance(result, PermissionResultAllow)
    request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_outside_cwd_prompts(tmp_path: Path) -> None:
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        policy=OpenCodePolicy(),
    )
    await can_use(
        "read", {"filePath": "/etc/passwd"}, _ctx("tu_read_out"),
    )
    request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_bash_in_sandbox_auto_approves(tmp_path: Path) -> None:
    """Containerized contexts auto-approve OpenCode's ``bash``."""
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        is_containerized=True,
        policy=OpenCodePolicy(),
    )
    result = await can_use(
        "bash", {"command": "ls"}, _ctx("tu_bash"),
    )
    assert isinstance(result, PermissionResultAllow)
    request_approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# apply_patch — multi-file mutating tool
# ---------------------------------------------------------------------------


_SAMPLE_PATCH = """\
*** Begin Patch
*** Update File: src/foo.py
@@
-old
+new
*** Add File: src/bar.py
+def bar(): pass
*** End Patch
"""


class TestApplyPatchSummary:
    def test_summarize_single_file_action(self) -> None:
        p = OpenCodePolicy()
        patch = "*** Begin Patch\n*** Add File: x.py\n+a\n*** End Patch\n"
        assert p.summarize(
            "apply_patch", {"patchText": patch}, None,
        ) == "add x.py"

    def test_summarize_multi_file_count(self) -> None:
        p = OpenCodePolicy()
        assert "2" in p.summarize(
            "apply_patch", {"patchText": _SAMPLE_PATCH}, None,
        )

    def test_summarize_empty_envelope(self) -> None:
        p = OpenCodePolicy()
        assert p.summarize(
            "apply_patch", {"patchText": ""}, None,
        ) == "(empty patch)"


class TestApplyPatchPathsWithin:
    def test_all_paths_within_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("old")
        p = OpenCodePolicy()
        assert p.multi_file_paths_within(
            "apply_patch",
            {"patchText": _SAMPLE_PATCH},
            str(tmp_path),
            [str(tmp_path)],
        ) is True

    def test_some_paths_outside_dir(self, tmp_path: Path) -> None:
        p = OpenCodePolicy()
        patch = (
            "*** Begin Patch\n"
            "*** Update File: /etc/foo\n@@\n-old\n+new\n"
            "*** End Patch\n"
        )
        assert p.multi_file_paths_within(
            "apply_patch",
            {"patchText": patch},
            str(tmp_path),
            [str(tmp_path)],
        ) is False

    def test_empty_patch_returns_false(self, tmp_path: Path) -> None:
        p = OpenCodePolicy()
        assert p.multi_file_paths_within(
            "apply_patch",
            {"patchText": ""},
            str(tmp_path),
            [str(tmp_path)],
        ) is False

    def test_non_apply_patch_returns_false(self, tmp_path: Path) -> None:
        p = OpenCodePolicy()
        assert p.multi_file_paths_within(
            "edit",
            {"filePath": str(tmp_path / "f.py")},
            str(tmp_path),
            [str(tmp_path)],
        ) is False


@pytest.mark.asyncio
async def test_apply_patch_in_sandbox_auto_approves(tmp_path: Path) -> None:
    """Containerized contexts auto-approve apply_patch (sandbox boundary)."""
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        is_containerized=True,
        policy=OpenCodePolicy(),
    )
    result = await can_use(
        "apply_patch", {"patchText": _SAMPLE_PATCH}, _ctx("tu_ap"),
    )
    assert isinstance(result, PermissionResultAllow)
    request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_patch_accept_all_edits_within_cwd(tmp_path: Path) -> None:
    """Accept-all-edits auto-approves apply_patch when every targeted
    path resolves inside the approved directories."""
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        is_edit_auto_approved=lambda: True,
        policy=OpenCodePolicy(),
    )
    result = await can_use(
        "apply_patch", {"patchText": _SAMPLE_PATCH}, _ctx("tu_ap2"),
    )
    assert isinstance(result, PermissionResultAllow)
    request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_patch_accept_all_edits_outside_cwd_prompts(
    tmp_path: Path,
) -> None:
    """Accept-all-edits does NOT auto-approve when paths leak outside."""
    request_approval = AsyncMock(return_value=False)
    can_use = make_can_use_tool(
        request_approval=request_approval,
        cwd=str(tmp_path),
        is_edit_auto_approved=lambda: True,
        policy=OpenCodePolicy(),
    )
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /etc/escape\n@@\n-old\n+new\n"
        "*** End Patch\n"
    )
    await can_use(
        "apply_patch", {"patchText": patch}, _ctx("tu_ap3"),
    )
    request_approval.assert_awaited_once()


# ---------------------------------------------------------------------------
# question — intentional bypass
# ---------------------------------------------------------------------------


def test_question_does_not_use_can_use_tool_path() -> None:
    """``handles_user_questions("question")`` returns False — the
    blocking lifecycle stays on the ``question.asked`` SSE arm.

    Regression guard: if someone later wires question through
    can_use_tool, the SSE arm and the policy will fight over the answer.
    """
    p = OpenCodePolicy()
    assert p.handles_user_questions("question") is False
    # The SDK's tool name is not understood either.
    assert p.handles_user_questions("AskUserQuestion") is False


# ---------------------------------------------------------------------------
# auto_approved_at_session_start — empty on OpenCode
# ---------------------------------------------------------------------------


def test_session_start_allowed_tools_is_empty() -> None:
    """OpenCode has no `allowedTools` equivalent — durable permissions
    flow through session-scoped category rules and the
    ``permission.asked.always`` arm (PR 4)."""
    assert OpenCodePolicy().auto_approved_at_session_start() == []


# ---------------------------------------------------------------------------
# Approval-text rendering
# ---------------------------------------------------------------------------


class TestApprovalText:
    def test_edit_reads_oldString_newString(self) -> None:
        """Renders an OpenCode edit input correctly — the regression
        guard for the camelCase param shape."""
        p = OpenCodePolicy()
        text = p.format_approval_text(
            "edit",
            {
                "filePath": "/cwd/x.py",
                "oldString": "foo",
                "newString": "bar",
            },
            "/cwd",
        )
        # Edit header present, diff body includes -foo/+bar.
        assert "Edit" in text
        assert "\\-foo" in text or "-foo" in text
        assert "\\+bar" in text or "+bar" in text

    def test_apply_patch_renders_multi_file(self) -> None:
        p = OpenCodePolicy()
        text = p.format_approval_text(
            "apply_patch", {"patchText": _SAMPLE_PATCH}, None,
        )
        assert "ApplyPatch" in text
        # Mentions both files (the dot is escaped by MarkdownV2 → ``\.``).
        assert "foo\\.py" in text
        assert "bar\\.py" in text


# ---------------------------------------------------------------------------
# Cross-backend invariant: shared categorical facts agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sdk_name, opencode_name", [
    ("Edit", "edit"),
    ("Write", "write"),
    ("Read", "read"),
    ("Glob", "glob"),
    ("Grep", "grep"),
    ("Bash", "bash"),
])
def test_shared_tool_policy_consistency(
    sdk_name: str, opencode_name: str,
) -> None:
    """The facts about Edit/Write/Read/Glob/Grep/Bash hold consistently
    across the two backends' equivalent tool names.

    This catches drift: if "Edit is mutating" changes on the SDK side
    and the OpenCode side is forgotten, this fails.
    """
    from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy

    sdk = ClaudeSdkPolicy()
    oc = OpenCodePolicy()
    assert sdk.is_mutating(sdk_name) == oc.is_mutating(opencode_name)
    assert sdk.is_path_scoped(sdk_name) == oc.is_path_scoped(opencode_name)
    assert sdk.is_file_targeted(sdk_name) == oc.is_file_targeted(opencode_name)
    assert (
        sdk.suppress_notification(sdk_name)
        == oc.suppress_notification(opencode_name)
    )
