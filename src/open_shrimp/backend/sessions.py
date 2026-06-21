"""Backend-neutral session metadata.

Matches the SDK's ``SDKSessionInfo`` field-for-field.  ``Backend.list_sessions``
returns these rows; each backend re-packs its native shape into them.
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
