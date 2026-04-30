"""Git staging operations for the review app.

Provides functions to stage and unstage individual hunks by
reconstructing unified diff patches and applying them via
`git apply --cached`.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from open_shrimp.review.git_diff import (
    Hunk,
)

logger = logging.getLogger(__name__)

# Per-directory locks to serialise git index operations.
# Without this, concurrent stage/unstage requests racing on the same
# repo cause "patch does not apply" errors.
_dir_locks: dict[str, asyncio.Lock] = {}


def _get_lock(cwd: str) -> asyncio.Lock:
    """Return (or create) the asyncio.Lock for the given directory."""
    if cwd not in _dir_locks:
        _dir_locks[cwd] = asyncio.Lock()
    return _dir_locks[cwd]


def _repo_cwd(base_cwd: str, hunk: Hunk) -> str:
    """Return the cwd of the repo that owns this hunk.

    For superproject hunks (``repo_path == ""``) this is ``base_cwd``;
    for submodule hunks it's ``base_cwd/<repo_path>``.
    """
    if not hunk.repo_path:
        return base_cwd
    return str(Path(base_cwd) / hunk.repo_path)


@dataclass
class StageResult:
    """Result of a stage/unstage operation."""

    ok: bool
    error: str | None = None
    stale: bool = False


def reconstruct_patch(hunk: Hunk) -> str:
    """Reconstruct a valid unified diff patch from a parsed hunk.

    The patch includes the file header (--- a/path, +++ b/path) and
    the hunk header + lines, suitable for piping into `git apply --cached`.

    Args:
        hunk: The parsed Hunk object.

    Returns:
        A string containing the full unified diff patch.
    """
    patch_lines: list[str] = []

    # File header.
    if hunk.is_new_file:
        patch_lines.append(f"diff --git a/{hunk.file_path} b/{hunk.file_path}")
        patch_lines.append("new file mode 100644")
        patch_lines.append("--- /dev/null")
        patch_lines.append(f"+++ b/{hunk.file_path}")
    elif hunk.is_deleted_file:
        patch_lines.append(f"diff --git a/{hunk.file_path} b/{hunk.file_path}")
        patch_lines.append("deleted file mode 100644")
        patch_lines.append(f"--- a/{hunk.file_path}")
        patch_lines.append("+++ /dev/null")
    else:
        patch_lines.append(f"diff --git a/{hunk.file_path} b/{hunk.file_path}")
        patch_lines.append(f"--- a/{hunk.file_path}")
        patch_lines.append(f"+++ b/{hunk.file_path}")

    # Hunk header and lines.
    patch_lines.append(hunk.hunk_header)

    for line in hunk.lines:
        if line.type == "add":
            patch_lines.append(f"+{line.content}")
        elif line.type == "delete":
            patch_lines.append(f"-{line.content}")
        elif line.type == "context":
            patch_lines.append(f" {line.content}")

    # Ensure patch ends with a newline.
    patch_text = "\n".join(patch_lines)
    if not patch_text.endswith("\n"):
        patch_text += "\n"

    return patch_text


async def stage_hunk(cwd: str, hunk: Hunk) -> StageResult:
    """Stage a single hunk by applying its patch to the index.

    Reconstructs the unified diff patch for the hunk and applies it
    via `git apply --cached`.

    Args:
        cwd: Working directory (must be inside a git repo).
        hunk: The hunk to stage.

    Returns:
        StageResult indicating success or failure.
    """
    patch = reconstruct_patch(hunk)
    target_cwd = _repo_cwd(cwd, hunk)

    async with _get_lock(target_cwd):
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--cached", "-",
            cwd=target_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=patch.encode())

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.error("git apply --cached failed: %s", error_msg)
        # If the patch doesn't apply, the working tree has likely changed.
        return StageResult(
            ok=False,
            error="Hunk is stale — the working tree has changed. Refresh to get current hunks.",
            stale=True,
        )

    return StageResult(ok=True)


async def unstage_hunk(cwd: str, hunk: Hunk) -> StageResult:
    """Unstage a single hunk by reverse-applying its patch from the index.

    Reconstructs the unified diff patch for the hunk and applies it
    in reverse via `git apply --cached -R`.

    Args:
        cwd: Working directory (must be inside a git repo).
        hunk: The hunk to unstage.

    Returns:
        StageResult indicating success or failure.
    """
    patch = reconstruct_patch(hunk)
    target_cwd = _repo_cwd(cwd, hunk)

    async with _get_lock(target_cwd):
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--cached", "-R", "-",
            cwd=target_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=patch.encode())

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.error("git apply --cached -R failed: %s", error_msg)
        # If the reverse patch doesn't apply, the working tree has likely changed.
        return StageResult(
            ok=False,
            error="Hunk is stale — the working tree has changed. Refresh to get current hunks.",
            stale=True,
        )

    # For new files, reverse-applying removes the file from the index
    # entirely, making it fully untracked.  We intentionally do NOT
    # re-add with --intent-to-add here — the file should go back to
    # being truly untracked so that `git status` shows it correctly.
    # The next `get_hunks()` call will re-add --intent-to-add if needed.

    return StageResult(ok=True)


async def stage_file(cwd: str, hunks: list[Hunk]) -> StageResult:
    """Stage all hunks for a file in a single lock acquisition.

    Concatenates patches for all provided hunks and applies them
    in one ``git apply --cached`` invocation.

    Args:
        cwd: Working directory (must be inside a git repo).
        hunks: The hunks to stage (should all belong to the same file).

    Returns:
        StageResult indicating success or failure.
    """
    if not hunks:
        return StageResult(ok=True)

    patches = [reconstruct_patch(h) for h in hunks]
    combined = "".join(patches)
    target_cwd = _repo_cwd(cwd, hunks[0])

    async with _get_lock(target_cwd):
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--cached", "-",
            cwd=target_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=combined.encode())

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.error("git apply --cached (batch) failed: %s", error_msg)
        return StageResult(
            ok=False,
            error="One or more hunks are stale — the working tree has changed. Refresh to get current hunks.",
            stale=True,
        )

    return StageResult(ok=True)


async def unstage_file(cwd: str, hunks: list[Hunk]) -> StageResult:
    """Unstage all hunks for a file in a single lock acquisition.

    Concatenates patches for all provided hunks and reverse-applies
    them in one ``git apply --cached -R`` invocation.

    Args:
        cwd: Working directory (must be inside a git repo).
        hunks: The hunks to unstage (should all belong to the same file).

    Returns:
        StageResult indicating success or failure.
    """
    if not hunks:
        return StageResult(ok=True)

    patches = [reconstruct_patch(h) for h in hunks]
    combined = "".join(patches)
    target_cwd = _repo_cwd(cwd, hunks[0])

    async with _get_lock(target_cwd):
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--cached", "-R", "-",
            cwd=target_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=combined.encode())

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.error("git apply --cached -R (batch) failed: %s", error_msg)
        return StageResult(
            ok=False,
            error="One or more hunks are stale — the working tree has changed. Refresh to get current hunks.",
            stale=True,
        )

    return StageResult(ok=True)


async def remove_intent_to_add(cwd: str, hunk: Hunk) -> StageResult:
    """Remove an intent-to-add index entry for a new file.

    When get_hunks() marks untracked files with ``git add --intent-to-add``
    so they appear in diffs, those entries linger in the index.  This
    function removes the entry via ``git rm --cached`` so the file goes
    back to being truly untracked.

    Only applicable to new files that have not been staged (i.e., still
    in the intent-to-add state).

    Args:
        cwd: Working directory (must be inside a git repo).
        hunk: The hunk representing the new file to clean up.

    Returns:
        StageResult indicating success or failure.
    """
    target_cwd = _repo_cwd(cwd, hunk)
    async with _get_lock(target_cwd):
        proc = await asyncio.create_subprocess_exec(
            "git", "rm", "--cached", "--", hunk.file_path,
            cwd=target_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.warning("git rm --cached failed for %s: %s", hunk.file_path, error_msg)
        # Not fatal — the file will just remain as intent-to-add.
        return StageResult(ok=False, error=error_msg)

    return StageResult(ok=True)
