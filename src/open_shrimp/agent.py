"""Attachment helpers and the backend-event type alias for OpenShrimp.

Holds the file-attachment plumbing (save/build-prompt/cleanup) shared by the
message handlers and the ``AgentEvent`` type alias.

SDK-message translation lives in the ``claude_sdk`` adapter
(``backend/claude_sdk/translate.py``); the persistent client path runs through
``client_manager`` and the ``Backend`` protocol.
"""

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from open_shrimp.backend import types as bt

logger = logging.getLogger(__name__)


@dataclass
class FileAttachment:
    """A file attachment to include in the prompt (image, PDF, etc.)."""

    data: bytes  # raw file bytes
    mime_type: str  # e.g. "image/jpeg", "application/pdf"
    filename: str | None = None  # original filename, if available



# Map MIME types to file extensions.
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".tar.gz",
}


@dataclass
class AgentResult:
    """Final result from an agent invocation."""

    session_id: str
    result_text: str


# Backend-neutral union of message types yielded by the agent path.  Every
# message is translated to one of these inside the backend client wrapper
# before it leaves, so consumers (stream.py, client_manager.py) never see
# backend-specific (e.g. SDK) types.
AgentEvent = bt.Message


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename for use in a temp file prefix.

    Strips path separators, null bytes, and other characters that are
    unsafe in file names, keeping only alphanumerics, hyphens, underscores,
    and dots.
    """
    return re.sub(r"[^\w.\-]", "_", name)


def save_attachments(
    attachments: list[FileAttachment],
    chat_id: int,
) -> list[Path]:
    """Save file attachments to temp files and return their paths.

    Files are saved into a per-chat subdirectory of
    :data:`~open_shrimp.hooks.ATTACHMENT_TEMP_DIR` so the canUseTool hook
    can auto-approve Read access for uploaded files without granting
    access to the entire ``/tmp`` tree.  Per-chat scoping prevents one
    agent session from accessing another session's uploads.

    Files are created with delete=False so they persist for the agent to
    read.  The caller is responsible for cleanup.
    """
    from open_shrimp.hooks import ATTACHMENT_TEMP_DIR

    chat_dir = ATTACHMENT_TEMP_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for att in attachments:
        ext = _MIME_TO_EXT.get(att.mime_type, ".bin")
        # Use sanitized original filename as part of the temp name if available.
        safe_name = _sanitize_filename(att.filename) if att.filename else ""
        prefix = f"openshrimp_{safe_name}_" if safe_name else "openshrimp_"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=prefix, delete=False,
            dir=chat_dir,
        )
        tmp.write(att.data)
        tmp.close()
        paths.append(Path(tmp.name))
        logger.info("Saved attachment to %s (%d bytes, %s)", tmp.name, len(att.data), att.mime_type)
    return paths


def build_prompt_with_attachments(prompt: str, attachment_paths: list[Path]) -> str:
    """Prepend file references to the user prompt."""
    parts: list[str] = []
    if len(attachment_paths) == 1:
        parts.append(
            f"The user attached a file. Read it from: {attachment_paths[0]}"
        )
    else:
        parts.append("The user attached files. Read them from:")
        for p in attachment_paths:
            parts.append(f"  - {p}")
    parts.append("")
    parts.append(prompt)
    return "\n".join(parts)


def cleanup_attachments(paths: list[Path]) -> None:
    """Remove temporary attachment files."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove temp file %s", p)
