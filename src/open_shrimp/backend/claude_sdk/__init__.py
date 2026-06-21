"""The Claude Agent SDK backend adapter.

Exposes ``ClaudeSdkBackend`` (the ``Backend`` implementation).  A subpackage —
``backend/opencode/`` and ``backend/pty_jsonl/`` slot in beside it later.
"""

from __future__ import annotations

from open_shrimp.backend.claude_sdk.backend import ClaudeSdkBackend

__all__ = ["ClaudeSdkBackend"]
