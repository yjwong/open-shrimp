"""Backend-neutral session metadata.

Matches the SDK's ``SDKSessionInfo`` field-for-field (verified).  Step 1 only
*defines* this; the SDK ``list_sessions`` call in ``handlers/commands.py`` is
not yet rewired (step 3's ``Backend.list_sessions`` owns that cutover).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionInfo:
    session_id: str
    summary: str
    last_modified: int
    created_at: int | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    git_branch: str | None = None
    file_size: int | None = None
    cwd: str | None = None
    tag: str | None = None


__all__ = ["SessionInfo"]
