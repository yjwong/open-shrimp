"""Git staging operations for the review app.

Provides functions to stage and unstage individual hunks by
reconstructing unified diff patches and applying them via
`git apply --cached`.
"""

import asyncio
import logging
from dataclasses import dataclass

from open_udang.review.git_diff import (
    Hunk,
)

logger = logging.getLogger(__name__)


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

    proc = await asyncio.create_subprocess_exec(
        "git", "apply", "--cached", "-",
        cwd=cwd,
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

    proc = await asyncio.create_subprocess_exec(
        "git", "apply", "--cached", "-R", "-",
        cwd=cwd,
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

    return StageResult(ok=True)
