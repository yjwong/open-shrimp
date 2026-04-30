"""Tests for git staging operations."""

import os
import textwrap

import pytest

from open_shrimp.review.git_diff import (
    Hunk,
    HunkLine,
    get_hunks,
    parse_diff,
)
from open_shrimp.review.git_stage import (
    StageResult,
    reconstruct_patch,
    stage_hunk,
    unstage_hunk,
)


# ---- Unit tests: patch reconstruction ----


class TestReconstructPatch:
    def test_simple_modification(self) -> None:
        hunk = Hunk(
            id="test123",
            file_path="src/main.py",
            language="python",
            is_new_file=False,
            is_deleted_file=False,
            hunk_header="@@ -10,3 +10,4 @@",
            lines=[
                HunkLine(type="context", old_no=10, new_no=10, content="import os"),
                HunkLine(type="add", old_no=None, new_no=11, content="import json"),
                HunkLine(type="context", old_no=11, new_no=12, content="import sys"),
                HunkLine(type="context", old_no=12, new_no=13, content=""),
            ],
            staged=False,
            is_binary=False,
        )
        patch = reconstruct_patch(hunk)
        assert "diff --git a/src/main.py b/src/main.py" in patch
        assert "--- a/src/main.py" in patch
        assert "+++ b/src/main.py" in patch
        assert "@@ -10,3 +10,4 @@" in patch
        assert " import os" in patch
        assert "+import json" in patch
        assert patch.endswith("\n")

    def test_new_file(self) -> None:
        hunk = Hunk(
            id="new123",
            file_path="new.py",
            language="python",
            is_new_file=True,
            is_deleted_file=False,
            hunk_header="@@ -0,0 +1,2 @@",
            lines=[
                HunkLine(type="add", old_no=None, new_no=1, content="line1"),
                HunkLine(type="add", old_no=None, new_no=2, content="line2"),
            ],
            staged=False,
            is_binary=False,
        )
        patch = reconstruct_patch(hunk)
        assert "new file mode 100644" in patch
        assert "--- /dev/null" in patch
        assert "+++ b/new.py" in patch

    def test_deleted_file(self) -> None:
        hunk = Hunk(
            id="del123",
            file_path="old.py",
            language="python",
            is_new_file=False,
            is_deleted_file=True,
            hunk_header="@@ -1,2 +0,0 @@",
            lines=[
                HunkLine(type="delete", old_no=1, new_no=None, content="line1"),
                HunkLine(type="delete", old_no=2, new_no=None, content="line2"),
            ],
            staged=False,
            is_binary=False,
        )
        patch = reconstruct_patch(hunk)
        assert "deleted file mode 100644" in patch
        assert "--- a/old.py" in patch
        assert "+++ /dev/null" in patch


# ---- Integration tests: real git repo ----


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with initial content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(
        f"cd {repo} && git init -b main "
        f"&& git config user.email 'test@test.com' "
        f"&& git config user.name 'Test'"
    )
    # Create initial files with multiple lines for context.
    (repo / "hello.py").write_text(
        "import os\nimport sys\n\ndef main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"
    )
    os.system(f"cd {repo} && git add . && git commit -m 'initial'")
    return str(repo)


@pytest.mark.asyncio
async def test_stage_hunk(git_repo: str) -> None:
    """Staging a hunk should move it to the index."""
    # Make an unstaged change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"
        )

    # Get hunks.
    result = await get_hunks(git_repo, include_untracked=False)
    assert result.total_hunks == 1
    hunk = result.hunks[0]
    assert hunk.staged is False

    # Stage the hunk.
    stage_result = await stage_hunk(git_repo, hunk)
    assert stage_result.ok is True

    # Verify it's now staged.
    result_after = await get_hunks(git_repo, include_untracked=False)
    staged_hunks = [h for h in result_after.hunks if h.staged]
    assert len(staged_hunks) == 1


@pytest.mark.asyncio
async def test_unstage_hunk(git_repo: str) -> None:
    """Unstaging a hunk should remove it from the index."""
    # Stage a change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"
        )
    os.system(f"cd {git_repo} && git add hello.py")

    # Get staged hunks.
    result = await get_hunks(git_repo, include_untracked=False)
    staged_hunks = [h for h in result.hunks if h.staged]
    assert len(staged_hunks) == 1
    hunk = staged_hunks[0]

    # Unstage it.
    unstage_result = await unstage_hunk(git_repo, hunk)
    assert unstage_result.ok is True

    # Verify it's now unstaged.
    result_after = await get_hunks(git_repo, include_untracked=False)
    staged_after = [h for h in result_after.hunks if h.staged]
    assert len(staged_after) == 0
    unstaged_after = [h for h in result_after.hunks if not h.staged]
    assert len(unstaged_after) == 1


@pytest.mark.asyncio
async def test_stage_then_unstage(git_repo: str) -> None:
    """Stage then unstage should return to clean index."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"
        )

    # Get and stage.
    result = await get_hunks(git_repo, include_untracked=False)
    hunk = result.hunks[0]
    await stage_hunk(git_repo, hunk)

    # Now get the staged hunk and unstage it.
    result_staged = await get_hunks(git_repo, include_untracked=False)
    staged_hunk = [h for h in result_staged.hunks if h.staged][0]
    await unstage_hunk(git_repo, staged_hunk)

    # Verify: no staged, only unstaged.
    result_final = await get_hunks(git_repo, include_untracked=False)
    staged = [h for h in result_final.hunks if h.staged]
    unstaged = [h for h in result_final.hunks if not h.staged]
    assert len(staged) == 0
    assert len(unstaged) == 1


@pytest.mark.asyncio
async def test_stage_new_file(git_repo: str) -> None:
    """Staging a hunk from a new (untracked) file."""
    with open(os.path.join(git_repo, "brand_new.py"), "w") as f:
        f.write("print('I am new')\n")

    # Get hunks including untracked.
    result = await get_hunks(git_repo, include_untracked=True)
    new_hunks = [h for h in result.hunks if h.file_path == "brand_new.py"]
    assert len(new_hunks) == 1
    hunk = new_hunks[0]

    # Stage it.
    stage_result = await stage_hunk(git_repo, hunk)
    assert stage_result.ok is True

    # Verify it's staged.
    result_after = await get_hunks(git_repo, include_untracked=True)
    staged_new = [
        h for h in result_after.hunks
        if h.file_path == "brand_new.py" and h.staged
    ]
    assert len(staged_new) == 1


@pytest.mark.asyncio
async def test_stale_hunk_detection(git_repo: str) -> None:
    """Staging a hunk whose patch no longer applies should return stale error."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"
        )

    # Get the hunk.
    result = await get_hunks(git_repo, include_untracked=False)
    hunk = result.hunks[0]

    # Stage the hunk so it's in the index, then try to stage the same
    # hunk again — the patch can't apply twice.
    stage_result = await stage_hunk(git_repo, hunk)
    assert stage_result.ok is True

    # Attempt to stage the same hunk again — should fail as stale.
    stage_result2 = await stage_hunk(git_repo, hunk)
    assert stage_result2.ok is False
    assert stage_result2.stale is True
    assert "stale" in stage_result2.error.lower()


# ---- Submodule integration tests ----


@pytest.fixture
def superproject_with_submodule(tmp_path):
    """Same shape as the fixture in test_git_diff.py — small enough to
    duplicate rather than share."""
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    os.system(
        f"cd {sub_origin} && git init -b main "
        f"&& git config user.email 'test@test.com' "
        f"&& git config user.name 'Test'"
    )
    (sub_origin / "inner.py").write_text(
        "import os\nimport sys\n\ndef main():\n    print('inner')\n"
    )
    os.system(
        f"cd {sub_origin} && git add . && git commit -m 'initial sub'"
    )

    super_repo = tmp_path / "super"
    super_repo.mkdir()
    os.system(
        f"cd {super_repo} && git init -b main "
        f"&& git config user.email 'test@test.com' "
        f"&& git config user.name 'Test' "
        f"&& git config protocol.file.allow always"
    )
    (super_repo / "top.py").write_text("print('top')\n")
    os.system(
        f"cd {super_repo} && git -c protocol.file.allow=always submodule add {sub_origin} sub "
        f"&& git add top.py "
        f"&& git commit -m 'initial super'"
    )
    return str(super_repo)


@pytest.mark.asyncio
async def test_stage_hunk_inside_submodule(superproject_with_submodule: str) -> None:
    """Staging a hunk for a submodule file should update the submodule's
    index, not the superproject's."""
    super_repo = superproject_with_submodule
    inner = os.path.join(super_repo, "sub", "inner.py")
    with open(inner, "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('inner')\n"
        )

    result = await get_hunks(super_repo, include_untracked=False)
    sub_hunks = [h for h in result.hunks if h.repo_path == "sub"]
    assert len(sub_hunks) == 1
    hunk = sub_hunks[0]
    assert hunk.staged is False

    stage_result = await stage_hunk(super_repo, hunk)
    assert stage_result.ok is True

    # The submodule's own index now has a staged change for inner.py.
    sub_status = os.popen(
        f"cd {os.path.join(super_repo, 'sub')} && git diff --cached --name-only"
    ).read().strip()
    assert sub_status == "inner.py"


@pytest.mark.asyncio
async def test_stage_then_unstage_inside_submodule(
    superproject_with_submodule: str,
) -> None:
    """Round-trip stage/unstage inside a submodule."""
    super_repo = superproject_with_submodule
    inner = os.path.join(super_repo, "sub", "inner.py")
    with open(inner, "w") as f:
        f.write(
            "import os\nimport sys\nimport json\n\ndef main():\n    print('inner')\n"
        )

    result = await get_hunks(super_repo, include_untracked=False)
    sub_hunks = [h for h in result.hunks if h.repo_path == "sub"]
    hunk = sub_hunks[0]
    await stage_hunk(super_repo, hunk)

    result_after = await get_hunks(super_repo, include_untracked=False)
    staged_sub = [
        h for h in result_after.hunks if h.repo_path == "sub" and h.staged
    ]
    assert len(staged_sub) == 1

    await unstage_hunk(super_repo, staged_sub[0])

    result_final = await get_hunks(super_repo, include_untracked=False)
    staged_sub_final = [
        h for h in result_final.hunks if h.repo_path == "sub" and h.staged
    ]
    unstaged_sub_final = [
        h for h in result_final.hunks if h.repo_path == "sub" and not h.staged
    ]
    assert staged_sub_final == []
    assert len(unstaged_sub_final) == 1


@pytest.mark.asyncio
async def test_stage_selective_hunk(git_repo: str) -> None:
    """When a file has multiple hunks, stage only one."""
    # Create a file with multiple sections.
    initial = "line1\nline2\nline3\n" + ("filler\n" * 20) + "line24\nline25\nline26\n"
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(initial)
    os.system(f"cd {git_repo} && git add hello.py && git commit -m 'multi-section'")

    # Modify both top and bottom.
    modified = "line1\nline2_modified\nline3\n" + ("filler\n" * 20) + "line24\nline25_modified\nline26\n"
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write(modified)

    result = await get_hunks(git_repo, include_untracked=False)
    assert result.total_hunks == 2

    # Stage only the first hunk.
    first_hunk = result.hunks[0]
    stage_result = await stage_hunk(git_repo, first_hunk)
    assert stage_result.ok is True

    # Verify: one staged, one unstaged.
    result_after = await get_hunks(git_repo, include_untracked=False)
    staged = [h for h in result_after.hunks if h.staged]
    unstaged = [h for h in result_after.hunks if not h.staged]
    assert len(staged) == 1
    assert len(unstaged) == 1
