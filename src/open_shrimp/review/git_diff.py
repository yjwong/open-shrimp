"""Git diff parsing and hunk extraction for the review app."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# File extension to language mapping for syntax highlighting.
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".toml": "toml",
    ".xml": "xml",
    ".lua": "lua",
    ".php": "php",
    ".swift": "swift",
    ".r": "r",
    ".R": "r",
    ".dockerfile": "dockerfile",
    ".proto": "protobuf",
}


@dataclass
class HunkLine:
    """A single line within a diff hunk."""

    type: str  # "add", "delete", or "context"
    old_no: int | None
    new_no: int | None
    content: str


@dataclass
class Hunk:
    """A parsed diff hunk with metadata.

    ``file_path`` is always relative to the repo that produced the diff
    (the superproject for ``repo_path == ""``, or relative to the
    submodule's worktree otherwise).  Patch reconstruction uses this
    in-repo path; the display path is ``repo_path + "/" + file_path``.
    """

    id: str
    file_path: str
    language: str
    is_new_file: bool
    is_deleted_file: bool
    hunk_header: str
    lines: list[HunkLine]
    staged: bool
    is_binary: bool
    is_empty: bool = False
    repo_path: str = ""


@dataclass
class HunkResult:
    """Paginated result of diff hunks."""

    total_hunks: int
    offset: int
    hunks: list[Hunk]


def detect_language(file_path: str) -> str:
    """Detect programming language from file extension."""
    # Handle Dockerfile specially (no extension).
    basename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    if basename.lower() in ("dockerfile", "containerfile"):
        return "dockerfile"
    if basename.lower() == "makefile":
        return "makefile"

    dot_idx = file_path.rfind(".")
    if dot_idx == -1:
        return "text"
    ext = file_path[dot_idx:]
    return _EXT_TO_LANGUAGE.get(ext, "text")


def generate_hunk_id(
    file_path: str,
    hunk_header: str,
    lines: list[HunkLine],
    repo_path: str = "",
) -> str:
    """Generate a stable, deterministic hash ID for a hunk.

    ``repo_path`` is included so that two repos (super + submodules)
    with same-named files produce distinct IDs.
    """
    content = repo_path + "\n" + file_path + "\n" + hunk_header + "\n"
    for line in lines:
        content += f"{line.type}:{line.content}\n"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# Regex for the unified diff file header.
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
# Regex for the hunk header.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# Regex for binary file detection.
_BINARY_RE = re.compile(r"^Binary files .* and .* differ$")
_LARGE_FILE_RE = re.compile(r"^Large file \(.+\) skipped$")


def parse_diff(diff_text: str, staged: bool, repo_path: str = "") -> list[Hunk]:
    """Parse unified diff output into structured Hunk objects.

    Args:
        diff_text: Raw output from `git diff`.
        staged: Whether this diff comes from `git diff --cached`.
        repo_path: Path of the repo that produced this diff, relative
            to the superproject.  Empty for the superproject; non-empty
            for submodules.

    Returns:
        List of parsed Hunk objects.
    """
    hunks: list[Hunk] = []
    if not diff_text.strip():
        return hunks

    lines = diff_text.split("\n")
    i = 0

    while i < len(lines):
        # Find the next diff header.
        header_match = _DIFF_HEADER_RE.match(lines[i])
        if not header_match:
            i += 1
            continue

        file_path_a = header_match.group(1)
        file_path_b = header_match.group(2)
        # Use the "b" path (destination) as the canonical file path.
        file_path = file_path_b
        i += 1

        is_new_file = False
        is_deleted_file = False
        is_binary = False

        # Parse extended header lines (new file mode, deleted file mode, etc.).
        while i < len(lines) and not lines[i].startswith("---") and not lines[i].startswith("@@"):
            if lines[i].startswith("new file mode"):
                is_new_file = True
            elif lines[i].startswith("deleted file mode"):
                is_deleted_file = True
            elif _BINARY_RE.match(lines[i]) or _LARGE_FILE_RE.match(lines[i]):
                is_binary = True
            # Check for next diff header — stop processing this file.
            if _DIFF_HEADER_RE.match(lines[i]):
                break
            i += 1

        if is_binary:
            # Binary file: create a single hunk with no lines.
            hunk_header = "(binary)"
            hunk_lines: list[HunkLine] = []
            hunk_id = generate_hunk_id(file_path, hunk_header, hunk_lines, repo_path)
            hunks.append(Hunk(
                id=hunk_id,
                file_path=file_path,
                language=detect_language(file_path),
                is_new_file=is_new_file,
                is_deleted_file=is_deleted_file,
                hunk_header=hunk_header,
                lines=hunk_lines,
                staged=staged,
                is_binary=True,
                repo_path=repo_path,
            ))
            continue

        # Skip --- and +++ lines.
        while i < len(lines) and (lines[i].startswith("---") or lines[i].startswith("+++")):
            i += 1

        # Check for empty file (no hunk headers follow).
        # This happens for new empty files like __init__.py where git produces
        # a diff header but no @@ hunks.
        if i >= len(lines) or not _HUNK_HEADER_RE.match(lines[i]):
            # Could be an empty file or we've hit the next diff header.
            # Only emit a hunk if the file is genuinely new or deleted
            # (otherwise it's just a mode change or similar with no content diff).
            if is_new_file or is_deleted_file:
                hunk_header = "(empty file)"
                hunk_lines: list[HunkLine] = []
                hunk_id = generate_hunk_id(file_path, hunk_header, hunk_lines, repo_path)
                hunks.append(Hunk(
                    id=hunk_id,
                    file_path=file_path,
                    language=detect_language(file_path),
                    is_new_file=is_new_file,
                    is_deleted_file=is_deleted_file,
                    hunk_header=hunk_header,
                    lines=hunk_lines,
                    staged=staged,
                    is_binary=False,
                    is_empty=True,
                    repo_path=repo_path,
                ))
            continue

        # Parse hunks for this file.
        while i < len(lines):
            hunk_match = _HUNK_HEADER_RE.match(lines[i])
            if not hunk_match:
                # Could be a new diff header or end of input.
                break

            hunk_header = lines[i]
            old_start = int(hunk_match.group(1))
            new_start = int(hunk_match.group(3))
            i += 1

            old_no = old_start
            new_no = new_start
            hunk_lines = []

            while i < len(lines):
                line = lines[i]
                # Stop at next hunk header or diff header.
                if _HUNK_HEADER_RE.match(line) or _DIFF_HEADER_RE.match(line):
                    break
                if line.startswith("+"):
                    hunk_lines.append(HunkLine(
                        type="add",
                        old_no=None,
                        new_no=new_no,
                        content=line[1:],
                    ))
                    new_no += 1
                elif line.startswith("-"):
                    hunk_lines.append(HunkLine(
                        type="delete",
                        old_no=old_no,
                        new_no=None,
                        content=line[1:],
                    ))
                    old_no += 1
                elif line.startswith(" "):
                    hunk_lines.append(HunkLine(
                        type="context",
                        old_no=old_no,
                        new_no=new_no,
                        content=line[1:],
                    ))
                    old_no += 1
                    new_no += 1
                elif line == "\\ No newline at end of file":
                    # Git marker, skip it.
                    pass
                else:
                    # Unknown line (e.g., empty line at end of diff).
                    # An empty line could be a context line with the trailing
                    # space stripped by git.
                    if line == "":
                        # Check if we're at the end of the diff output.
                        # Peek ahead: if next line is a hunk/diff header or
                        # EOF, this is the end of the hunk.
                        if i + 1 >= len(lines) or _HUNK_HEADER_RE.match(lines[i + 1]) or _DIFF_HEADER_RE.match(lines[i + 1]):
                            i += 1
                            break
                        # Otherwise, treat as context line with empty content.
                        hunk_lines.append(HunkLine(
                            type="context",
                            old_no=old_no,
                            new_no=new_no,
                            content="",
                        ))
                        old_no += 1
                        new_no += 1
                i += 1

            if hunk_lines:
                hunk_id = generate_hunk_id(file_path, hunk_header, hunk_lines, repo_path)
                hunks.append(Hunk(
                    id=hunk_id,
                    file_path=file_path,
                    language=detect_language(file_path),
                    is_new_file=is_new_file,
                    is_deleted_file=is_deleted_file,
                    hunk_header=hunk_header,
                    lines=hunk_lines,
                    staged=staged,
                    is_binary=False,
                    repo_path=repo_path,
                ))

    return hunks


async def _run_git(cwd: str, *args: str) -> tuple[str, str, int]:
    """Run a git command as an async subprocess.

    Returns:
        Tuple of (stdout, stderr, returncode).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


async def _get_untracked_files(cwd: str) -> list[str]:
    """Get list of untracked files in the working directory."""
    stdout, _, rc = await _run_git(cwd, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        return []
    return [f for f in stdout.strip().split("\n") if f]


def _is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    """Check if a file is binary by looking for null bytes in the first chunk.

    Uses the same heuristic as git: a file is binary if it contains a
    null byte in the first ``sample_size`` bytes.
    """
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(sample_size)
    except OSError:
        return False


_MAX_UNTRACKED_DIFF_SIZE = 1_000_000  # 1 MB


async def _diff_untracked_files(cwd: str, files: list[str]) -> str:
    """Generate unified diff output for untracked files without touching the index.

    Binary files are detected cheaply (first 8KB null-byte check) and get
    a synthetic diff header.  Large text files (>1 MB) are skipped with a
    synthetic header to avoid reading gigabytes of data.  Remaining text
    files are diffed via ``git diff --no-index`` as usual.
    """
    if not files:
        return ""

    text_files: list[str] = []
    skipped_diffs: list[str] = []
    for file_path in files:
        full_path = Path(cwd) / file_path
        if _is_binary_file(full_path):
            skipped_diffs.append(
                f"diff --git a/{file_path} b/{file_path}\n"
                f"new file mode 100644\n"
                f"Binary files /dev/null and b/{file_path} differ\n"
            )
        elif (file_size := full_path.stat().st_size) > _MAX_UNTRACKED_DIFF_SIZE:
            size_mb = file_size / 1_000_000
            skipped_diffs.append(
                f"diff --git a/{file_path} b/{file_path}\n"
                f"new file mode 100644\n"
                f"Large file ({size_mb:.1f} MB) skipped\n"
            )
        else:
            text_files.append(file_path)

    # Diff text files concurrently via git.
    async def _diff_one(file_path: str) -> str:
        stdout, _, rc = await _run_git(
            cwd, "diff", "--no-index", "--no-color", "-U3", "--",
            "/dev/null", file_path,
        )
        # --no-index exits 1 when there are differences (not an error).
        if rc not in (0, 1):
            logger.warning("git diff --no-index failed for %s (rc=%d)", file_path, rc)
            return ""
        return stdout

    text_diffs = await asyncio.gather(*[_diff_one(f) for f in text_files])
    all_diffs = skipped_diffs + [d for d in text_diffs if d]
    return "\n".join(all_diffs)


async def _get_submodule_paths(cwd: str) -> list[str]:
    """Return initialized submodule paths relative to ``cwd``.

    Uses ``git submodule foreach --recursive`` so nested submodules are
    discovered.  Returns an empty list when there are no submodules or
    when the project doesn't use them at all (no ``.gitmodules``).
    """
    stdout, _, rc = await _run_git(
        cwd, "submodule", "foreach", "--recursive", "--quiet",
        "echo $displaypath",
    )
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


async def _get_hunks_for_repo(
    cwd: str,
    repo_path: str,
    include_untracked: bool,
) -> list[Hunk]:
    """Collect staged + unstaged + untracked hunks for one repo.

    ``cwd`` is the absolute path of the repo's working tree.
    ``repo_path`` is its location relative to the superproject (empty
    string for the superproject itself).
    """
    # For the superproject we suppress the noisy "submodule is dirty"
    # entries — those edits surface inside each submodule's own diff.
    extra_args = ["--ignore-submodules=dirty"] if not repo_path else []

    unstaged_task = asyncio.ensure_future(
        _run_git(cwd, "diff", "--no-color", "-U3", *extra_args)
    )
    staged_task = asyncio.ensure_future(
        _run_git(cwd, "diff", "--cached", "--no-color", "-U3", *extra_args)
    )

    untracked_task: asyncio.Task[str] | None = None
    if include_untracked:
        untracked = await _get_untracked_files(cwd)
        if untracked:
            untracked_task = asyncio.ensure_future(
                _diff_untracked_files(cwd, untracked)
            )

    (unstaged_out, unstaged_err, unstaged_rc) = await unstaged_task
    (staged_out, staged_err, staged_rc) = await staged_task

    untracked_diff = ""
    if untracked_task is not None:
        untracked_diff = await untracked_task

    if unstaged_rc != 0:
        logger.warning("git diff failed in %s: %s", cwd, unstaged_err.strip())
    if staged_rc != 0:
        logger.warning(
            "git diff --cached failed in %s: %s", cwd, staged_err.strip()
        )

    unstaged = parse_diff(unstaged_out, staged=False, repo_path=repo_path)
    staged = parse_diff(staged_out, staged=True, repo_path=repo_path)
    untracked_hunks = (
        parse_diff(untracked_diff, staged=False, repo_path=repo_path)
        if untracked_diff else []
    )
    return staged + unstaged + untracked_hunks


async def get_hunks(
    cwd: str,
    offset: int = 0,
    limit: int = 20,
    include_untracked: bool = True,
) -> HunkResult:
    """Get paginated diff hunks from a git working directory.

    Combines staged changes, unstaged changes, and (optionally) untracked
    files.  When the working directory is a superproject containing
    submodules, hunks from each initialised submodule are merged into
    the same flat result, with their ``repo_path`` set to the
    submodule's location relative to the superproject.

    Args:
        cwd: Working directory (must be inside a git repo).
        offset: Number of hunks to skip.
        limit: Maximum number of hunks to return.
        include_untracked: Whether to include untracked files.

    Returns:
        HunkResult with total count and paginated hunk list.

    Raises:
        ValueError: If cwd is not inside a git repository.
    """
    # Verify we're inside a git repository before running diff commands.
    # Without this check, git falls back to --no-index mode which doesn't
    # support --cached and produces confusing errors.
    _, _, rc = await _run_git(cwd, "rev-parse", "--git-dir")
    if rc != 0:
        raise ValueError(
            f"Directory is not inside a git repository: {cwd}"
        )

    submodule_paths = await _get_submodule_paths(cwd)

    # Fan out per repo: superproject + each initialised submodule.
    repo_tasks = [_get_hunks_for_repo(cwd, "", include_untracked)]
    for sub in submodule_paths:
        sub_cwd = str(Path(cwd) / sub)
        repo_tasks.append(_get_hunks_for_repo(sub_cwd, sub, include_untracked))

    per_repo_hunks = await asyncio.gather(*repo_tasks)

    # Combine hunks grouped by (repo_path, file_path) so that all hunks
    # for the same file are contiguous (staged before unstaged within
    # each file).  This prevents pagination from splitting a file's
    # hunks across pages.
    from collections import OrderedDict

    hunks_by_file: OrderedDict[tuple[str, str], list[Hunk]] = OrderedDict()
    for repo_hunks in per_repo_hunks:
        for h in repo_hunks:
            hunks_by_file.setdefault((h.repo_path, h.file_path), []).append(h)

    all_hunks: list[Hunk] = []
    for file_hunks in hunks_by_file.values():
        all_hunks.extend(file_hunks)

    total = len(all_hunks)

    # Apply pagination.
    paginated = all_hunks[offset:offset + limit]

    return HunkResult(
        total_hunks=total,
        offset=offset,
        hunks=paginated,
    )
