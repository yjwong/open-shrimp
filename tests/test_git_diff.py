"""Tests for git diff parsing and hunk extraction."""

import os
import sys
import textwrap

import pytest

from open_shrimp.review.git_diff import (
    Hunk,
    HunkLine,
    HunkResult,
    detect_language,
    generate_hunk_id,
    get_hunks,
    parse_diff,
)


# ---- Unit tests: language detection ----


class TestDetectLanguage:
    def test_python(self) -> None:
        assert detect_language("src/main.py") == "python"

    def test_typescript(self) -> None:
        assert detect_language("components/App.tsx") == "typescript"

    def test_javascript(self) -> None:
        assert detect_language("index.js") == "javascript"

    def test_go(self) -> None:
        assert detect_language("cmd/server/main.go") == "go"

    def test_rust(self) -> None:
        assert detect_language("src/lib.rs") == "rust"

    def test_yaml(self) -> None:
        assert detect_language("config.yaml") == "yaml"
        assert detect_language("config.yml") == "yaml"

    def test_json(self) -> None:
        assert detect_language("package.json") == "json"

    def test_markdown(self) -> None:
        assert detect_language("README.md") == "markdown"

    def test_dockerfile(self) -> None:
        assert detect_language("Dockerfile") == "dockerfile"
        assert detect_language("path/to/Dockerfile") == "dockerfile"

    def test_makefile(self) -> None:
        assert detect_language("Makefile") == "makefile"

    def test_unknown_extension(self) -> None:
        assert detect_language("file.xyz") == "text"

    def test_no_extension(self) -> None:
        assert detect_language("somefile") == "text"


# ---- Unit tests: hunk ID generation ----


class TestGenerateHunkId:
    def test_deterministic(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        id1 = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines)
        id2 = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines)
        assert id1 == id2

    def test_different_content(self) -> None:
        lines_a = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        lines_b = [HunkLine(type="add", old_no=None, new_no=1, content="world")]
        id_a = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines_a)
        id_b = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines_b)
        assert id_a != id_b

    def test_different_file(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        id_a = generate_hunk_id("a.py", "@@ -0,0 +1 @@", lines)
        id_b = generate_hunk_id("b.py", "@@ -0,0 +1 @@", lines)
        assert id_a != id_b

    def test_length(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="x")]
        hunk_id = generate_hunk_id("f.py", "@@", lines)
        assert len(hunk_id) == 16


# ---- Unit tests: diff parsing ----


SIMPLE_DIFF = textwrap.dedent("""\
    diff --git a/src/main.py b/src/main.py
    index abc1234..def5678 100644
    --- a/src/main.py
    +++ b/src/main.py
    @@ -10,6 +10,8 @@ import os
     import os
     import sys
    +import json
    +import yaml

     def main():
""")


class TestParseDiff:
    def test_simple_modification(self) -> None:
        hunks = parse_diff(SIMPLE_DIFF, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.file_path == "src/main.py"
        assert hunk.language == "python"
        assert hunk.is_new_file is False
        assert hunk.is_deleted_file is False
        assert hunk.staged is False
        assert hunk.is_binary is False
        assert hunk.hunk_header == "@@ -10,6 +10,8 @@ import os"

        # Check lines.
        add_lines = [l for l in hunk.lines if l.type == "add"]
        assert len(add_lines) == 2
        assert add_lines[0].content == "import json"
        assert add_lines[1].content == "import yaml"

        context_lines = [l for l in hunk.lines if l.type == "context"]
        assert len(context_lines) >= 2

    def test_new_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/new_file.py b/new_file.py
            new file mode 100644
            index 0000000..abc1234
            --- /dev/null
            +++ b/new_file.py
            @@ -0,0 +1,3 @@
            +#!/usr/bin/env python
            +
            +print("hello")
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_new_file is True
        assert hunk.file_path == "new_file.py"
        assert len(hunk.lines) == 3
        assert all(l.type == "add" for l in hunk.lines)

    def test_deleted_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/old_file.py b/old_file.py
            deleted file mode 100644
            index abc1234..0000000
            --- a/old_file.py
            +++ /dev/null
            @@ -1,2 +0,0 @@
            -import os
            -print("bye")
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_deleted_file is True
        assert hunk.file_path == "old_file.py"
        assert all(l.type == "delete" for l in hunk.lines)

    def test_binary_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/image.png b/image.png
            index abc1234..def5678 100644
            Binary files a/image.png and b/image.png differ
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_binary is True
        assert hunk.file_path == "image.png"
        assert hunk.lines == []
        assert hunk.hunk_header == "(binary)"

    def test_multiple_hunks_same_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/big.py b/big.py
            index abc1234..def5678 100644
            --- a/big.py
            +++ b/big.py
            @@ -1,3 +1,4 @@
             line1
            +added_top
             line2
             line3
            @@ -50,3 +51,4 @@
             line50
            +added_bottom
             line51
             line52
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 2
        assert hunks[0].hunk_header.startswith("@@ -1,3")
        assert hunks[1].hunk_header.startswith("@@ -50,3")

    def test_multiple_files(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/a.py b/a.py
            index abc..def 100644
            --- a/a.py
            +++ b/a.py
            @@ -1,2 +1,3 @@
             line1
            +new_in_a
             line2
            diff --git a/b.js b/b.js
            index abc..def 100644
            --- a/b.js
            +++ b/b.js
            @@ -1,2 +1,3 @@
             const x = 1;
            +const y = 2;
             console.log(x);
        """)
        hunks = parse_diff(diff, staged=True)
        assert len(hunks) == 2
        assert hunks[0].file_path == "a.py"
        assert hunks[0].language == "python"
        assert hunks[0].staged is True
        assert hunks[1].file_path == "b.js"
        assert hunks[1].language == "javascript"
        assert hunks[1].staged is True

    def test_empty_diff(self) -> None:
        hunks = parse_diff("", staged=False)
        assert hunks == []

    def test_whitespace_only_diff(self) -> None:
        hunks = parse_diff("  \n\n  ", staged=False)
        assert hunks == []

    def test_line_numbers(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/f.py b/f.py
            index abc..def 100644
            --- a/f.py
            +++ b/f.py
            @@ -5,4 +5,5 @@
             context_line
            -deleted_line
            +added_line1
            +added_line2
             another_context
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        lines = hunks[0].lines
        # context_line: old=5, new=5
        assert lines[0].type == "context"
        assert lines[0].old_no == 5
        assert lines[0].new_no == 5
        # deleted_line: old=6, new=None
        assert lines[1].type == "delete"
        assert lines[1].old_no == 6
        assert lines[1].new_no is None
        # added_line1: old=None, new=6
        assert lines[2].type == "add"
        assert lines[2].old_no is None
        assert lines[2].new_no == 6
        # added_line2: old=None, new=7
        assert lines[3].type == "add"
        assert lines[3].old_no is None
        assert lines[3].new_no == 7
        # another_context: old=7, new=8
        assert lines[4].type == "context"
        assert lines[4].old_no == 7
        assert lines[4].new_no == 8

    def test_rename(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/old_name.py b/new_name.py
            similarity index 90%
            rename from old_name.py
            rename to new_name.py
            index abc..def 100644
            --- a/old_name.py
            +++ b/new_name.py
            @@ -1,3 +1,3 @@
             line1
            -old_content
            +new_content
             line3
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        # Should use the "b" (destination) path.
        assert hunks[0].file_path == "new_name.py"


# ---- Integration tests: real git repo ----


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f"cd {repo} && git init -b main && git config user.email 'test@test.com' && git config user.name 'Test'")
    # Create initial file and commit.
    (repo / "hello.py").write_text("print('hello')\n")
    os.system(f"cd {repo} && git add . && git commit -m 'initial'")
    return str(repo)


@pytest.mark.asyncio
async def test_get_hunks_no_changes(git_repo: str) -> None:
    """No changes should return empty result."""
    result = await get_hunks(git_repo)
    assert result.total_hunks == 0
    assert result.hunks == []
    assert result.offset == 0


@pytest.mark.asyncio
async def test_get_hunks_unstaged_changes(git_repo: str) -> None:
    """Unstaged modifications should appear as hunks."""
    # Modify a tracked file.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('hello')\nprint('world')\n")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 1
    hunk = result.hunks[0]
    assert hunk.file_path == "hello.py"
    assert hunk.staged is False
    assert hunk.language == "python"
    # Should have an add line for "print('world')".
    add_lines = [l for l in hunk.lines if l.type == "add"]
    assert any("world" in l.content for l in add_lines)


@pytest.mark.asyncio
async def test_get_hunks_staged_changes(git_repo: str) -> None:
    """Staged changes should appear with staged=True."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('hello')\nprint('staged')\n")
    os.system(f"cd {git_repo} && git add hello.py")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 1
    hunk = result.hunks[0]
    assert hunk.staged is True


@pytest.mark.asyncio
async def test_get_hunks_mixed_staged_unstaged(git_repo: str) -> None:
    """Both staged and unstaged changes should appear."""
    # Stage a change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('staged_change')\n")
    os.system(f"cd {git_repo} && git add hello.py")

    # Make another unstaged change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('staged_change')\nprint('unstaged_change')\n")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 2
    staged = [h for h in result.hunks if h.staged]
    unstaged = [h for h in result.hunks if not h.staged]
    assert len(staged) == 1
    assert len(unstaged) == 1


@pytest.mark.asyncio
async def test_get_hunks_untracked_files(git_repo: str) -> None:
    """Untracked files should appear when include_untracked=True."""
    with open(os.path.join(git_repo, "new_file.txt"), "w") as f:
        f.write("new content\n")

    result = await get_hunks(git_repo, include_untracked=True)
    assert result.total_hunks >= 1
    new_hunks = [h for h in result.hunks if h.file_path == "new_file.txt"]
    assert len(new_hunks) == 1
    assert new_hunks[0].is_new_file is True


@pytest.mark.asyncio
async def test_get_hunks_untracked_non_utf8(git_repo: str) -> None:
    """A latin-1 byte in an untracked file must not fail the whole request,
    and must not reach a stageable hunk either — the bytes a patch rebuilt
    from it would carry are U+FFFD, not the file's."""
    with open(os.path.join(git_repo, "cp1252.log"), "wb") as f:
        f.write(b"start \x97 end\n")

    result = await get_hunks(git_repo, include_untracked=True)
    hunks = [h for h in result.hunks if h.file_path == "cp1252.log"]
    assert len(hunks) == 1
    assert hunks[0].is_binary is True
    assert hunks[0].lines == []


@pytest.mark.asyncio
async def test_get_hunks_marks_a_bad_byte_past_the_sample(git_repo: str) -> None:
    """The 8KB sample is a heuristic; a bad byte beyond it still has to be
    caught, or its hunk stays stageable."""
    with open(os.path.join(git_repo, "late.log"), "wb") as f:
        f.write(b"clean line\n" * 2000)
        f.write(b"late \x97 byte\n")

    result = await get_hunks(git_repo, include_untracked=True, limit=1000)
    hunks = [h for h in result.hunks if h.file_path == "late.log"]
    assert hunks, "the file passed the sample check and should have been diffed"
    assert any(h.is_lossy for h in hunks)
    # Only the hunk that actually carries the damage is disqualified.
    for hunk in hunks:
        carries = any("�" in line.content for line in hunk.lines)
        assert hunk.is_lossy is carries


@pytest.mark.asyncio
async def test_a_multibyte_char_across_the_sample_boundary_is_still_text(
    git_repo: str,
) -> None:
    """The sample cuts at a fixed offset, so it can land mid-character. That
    is a truncated sequence, not an invalid one, and a UTF-8 file must not be
    demoted to binary for it."""
    # 8191 ASCII bytes then a 3-byte character: the sample ends one byte in.
    body = ("a" * 8191) + "€" + "  tail\n"
    with open(os.path.join(git_repo, "boundary.txt"), "w", encoding="utf-8") as f:
        f.write(body)

    result = await get_hunks(git_repo, include_untracked=True, limit=1000)
    hunks = [h for h in result.hunks if h.file_path == "boundary.txt"]
    assert hunks
    assert not any(h.is_binary for h in hunks)
    assert not any(h.is_lossy for h in hunks)


def test_lossy_output_only_marks_the_hunks_that_carry_it() -> None:
    """The flag says replacement happened in this output; the character says
    where.  A clean hunk from the same diff stays stageable."""
    diff = textwrap.dedent("""\
        diff --git a/clean.txt b/clean.txt
        --- a/clean.txt
        +++ b/clean.txt
        @@ -1,1 +1,1 @@
        -old
        +new
        diff --git a/dirty.txt b/dirty.txt
        --- a/dirty.txt
        +++ b/dirty.txt
        @@ -1,1 +1,1 @@
        -old
        +new � here
        """)

    hunks = parse_diff(diff, staged=False, lossy=True)

    by_path = {h.file_path: h for h in hunks}
    assert by_path["clean.txt"].is_lossy is False
    assert by_path["dirty.txt"].is_lossy is True


def test_a_clean_decode_never_marks_a_hunk() -> None:
    """A genuine U+FFFD in a UTF-8 source file is just a character."""
    diff = textwrap.dedent("""\
        diff --git a/doc.md b/doc.md
        --- a/doc.md
        +++ b/doc.md
        @@ -1,1 +1,1 @@
        -old
        +the replacement character is �
        """)

    assert parse_diff(diff, staged=False, lossy=False)[0].is_lossy is False


@pytest.mark.skipif(
    sys.platform == "win32", reason="resource.setrlimit is POSIX-only"
)
@pytest.mark.asyncio
async def test_get_hunks_untracked_many_files(git_repo: str) -> None:
    """Hundreds of untracked files must stay within the process fd limit."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    try:
        for i in range(400):
            with open(os.path.join(git_repo, f"f{i}.txt"), "w") as f:
                f.write(f"content {i}\n")

        result = await get_hunks(git_repo, include_untracked=True, limit=1000)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    assert len([h for h in result.hunks if h.file_path.startswith("f")]) == 400


def test_untracked_diff_survives_a_fresh_event_loop(git_repo: str) -> None:
    """The process is not one loop for life — the menu-bar app builds a new
    one per Start.  An asyncio primitive latches the loop it is first
    contended on, so the fan-out's gate must not outlive a single call.
    """
    import asyncio

    for i in range(40):
        with open(os.path.join(git_repo, f"g{i}.txt"), "w") as f:
            f.write(f"content {i}\n")

    def run() -> HunkResult:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                get_hunks(git_repo, include_untracked=True, limit=1000)
            )
        finally:
            loop.close()

    first, second = run(), run()

    assert len([h for h in first.hunks if h.file_path.startswith("g")]) == 40
    assert len([h for h in second.hunks if h.file_path.startswith("g")]) == 40


@pytest.mark.asyncio
async def test_get_hunks_untracked_excluded(git_repo: str) -> None:
    """Untracked files should not appear when include_untracked=False."""
    with open(os.path.join(git_repo, "new_file.txt"), "w") as f:
        f.write("new content\n")

    result = await get_hunks(git_repo, include_untracked=False)
    new_hunks = [h for h in result.hunks if h.file_path == "new_file.txt"]
    assert len(new_hunks) == 0


@pytest.mark.asyncio
async def test_get_hunks_pagination(git_repo: str) -> None:
    """Pagination should work correctly."""
    # Create several changes across multiple files.
    for i in range(5):
        with open(os.path.join(git_repo, f"file{i}.py"), "w") as f:
            f.write(f"content_{i}\n")

    result_all = await get_hunks(git_repo, include_untracked=True)
    total = result_all.total_hunks
    assert total >= 5

    # First page.
    result_p1 = await get_hunks(git_repo, offset=0, limit=2, include_untracked=True)
    assert len(result_p1.hunks) == 2
    assert result_p1.total_hunks == total
    assert result_p1.offset == 0

    # Second page.
    result_p2 = await get_hunks(git_repo, offset=2, limit=2, include_untracked=True)
    assert len(result_p2.hunks) == 2
    assert result_p2.offset == 2

    # Ensure no overlap.
    ids_p1 = {h.id for h in result_p1.hunks}
    ids_p2 = {h.id for h in result_p2.hunks}
    assert ids_p1.isdisjoint(ids_p2)


# ---- Submodule tests ----


@pytest.fixture
def superproject_with_submodule(tmp_path):
    """Create a superproject with one initialised submodule.

    Layout:
        super/
            top.py
            sub/  <-- submodule (pointing at sub_origin)
                inner.py
    """
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    os.system(
        f"cd {sub_origin} && git init -b main "
        f"&& git config user.email 'test@test.com' "
        f"&& git config user.name 'Test'"
    )
    (sub_origin / "inner.py").write_text("print('inner')\n")
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
async def test_get_hunks_submodule_inner_edit(superproject_with_submodule: str) -> None:
    """Edits inside a submodule's tracked file should appear as a hunk
    with repo_path set to the submodule's path."""
    super_repo = superproject_with_submodule
    inner = os.path.join(super_repo, "sub", "inner.py")
    with open(inner, "w") as f:
        f.write("print('inner')\nprint('edited')\n")

    result = await get_hunks(super_repo)
    sub_hunks = [h for h in result.hunks if h.repo_path == "sub"]
    assert len(sub_hunks) == 1
    assert sub_hunks[0].file_path == "inner.py"
    assert sub_hunks[0].staged is False


@pytest.mark.asyncio
async def test_get_hunks_super_unaffected_by_dirty_submodule(
    superproject_with_submodule: str,
) -> None:
    """A dirty submodule should not produce a noisy hunk in the
    superproject's diff (we use --ignore-submodules=dirty)."""
    super_repo = superproject_with_submodule
    inner = os.path.join(super_repo, "sub", "inner.py")
    with open(inner, "w") as f:
        f.write("print('inner edited')\n")

    result = await get_hunks(super_repo)
    super_hunks = [h for h in result.hunks if h.repo_path == ""]
    # Only inner-submodule edit; super has no own changes.
    assert super_hunks == []


@pytest.mark.asyncio
async def test_get_hunks_submodule_untracked_file(
    superproject_with_submodule: str,
) -> None:
    """A new untracked file inside a submodule should show up under
    repo_path == 'sub'."""
    super_repo = superproject_with_submodule
    new_inside = os.path.join(super_repo, "sub", "added.py")
    with open(new_inside, "w") as f:
        f.write("print('new in submodule')\n")

    result = await get_hunks(super_repo, include_untracked=True)
    matching = [
        h for h in result.hunks
        if h.repo_path == "sub" and h.file_path == "added.py"
    ]
    assert len(matching) == 1
    assert matching[0].is_new_file is True


@pytest.mark.asyncio
async def test_get_hunks_super_and_sub_combined(
    superproject_with_submodule: str,
) -> None:
    """Edits in both the superproject and a submodule should appear in
    the same flat result list, distinguishable by repo_path."""
    super_repo = superproject_with_submodule
    with open(os.path.join(super_repo, "top.py"), "w") as f:
        f.write("print('top edited')\n")
    with open(os.path.join(super_repo, "sub", "inner.py"), "w") as f:
        f.write("print('inner edited')\n")

    result = await get_hunks(super_repo)
    super_paths = {(h.repo_path, h.file_path) for h in result.hunks}
    assert ("", "top.py") in super_paths
    assert ("sub", "inner.py") in super_paths


@pytest.mark.asyncio
async def test_get_hunks_no_submodules_unaffected(git_repo: str) -> None:
    """A repo with no submodules should behave exactly as before:
    all hunks have repo_path == ''."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('changed')\n")
    result = await get_hunks(git_repo)
    assert all(h.repo_path == "" for h in result.hunks)
