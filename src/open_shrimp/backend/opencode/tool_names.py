"""Permission categories OpenShrimp owns at session-create.

Tool names flow through OpenCode in native form — ``ToolUseBlock.name``
carries the wire name (``bash``, ``read``, ``edit``, …) and the OpenCode
policy answers questions about that vocabulary.  No name translation
happens at the ingest boundary.

``OPENCODE_PERMISSION_CATEGORIES`` is not a translation table; it tells
OpenCode *which categories OpenShrimp owns* at session-create (so we can
rewrite their default ``allow`` to ``ask`` and route through
``can_use_tool``).  ``question`` is intentionally listed: its default
needs to be rewritten to ``ask``, but its blocking lifecycle is handled
by the native ``question.asked`` SSE arm in ``translate.py``, not the
permission bridge.
"""

from __future__ import annotations


OPENCODE_PERMISSION_CATEGORIES: tuple[str, ...] = (
    "bash",
    "read",
    "edit",
    "question",
    "webfetch",
    "webwrite",
    "external_directory",
)


__all__ = ["OPENCODE_PERMISSION_CATEGORIES"]
