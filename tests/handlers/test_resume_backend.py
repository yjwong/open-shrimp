"""``/resume`` lists sessions from the active context's backend.

When a context's ``backend:`` is overridden the ``list_sessions`` call
must hit *that* backend, not the top-level default — so the listing
matches what would actually serve the next turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import open_shrimp.client_manager as cm
from open_shrimp.db import ChatScope
from open_shrimp.handlers.commands import _build_resume_page

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeCtx:
    """Stand-in for ``ContextConfig`` carrying only the fields the resume
    page reads."""

    directory: str = "/tmp/fake"
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    additional_directories: list[str] = field(default_factory=list)
    default_for_chats: list[int] = field(default_factory=list)
    locked_for_chats: list[int] = field(default_factory=list)
    container: Any = None
    sandbox: Any = None
    mcp: dict[str, Any] = field(default_factory=dict)
    backend: str | None = None


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    cm._active_sessions.clear()
    monkeypatch.setattr(cm, "_default_backend", None, raising=False)
    yield
    cm._active_sessions.clear()


async def test_resume_lists_from_context_backend(
    monkeypatch: pytest.MonkeyPatch,
):
    """The per-context backend serves the listing, not the top-level default."""
    sdk = MagicMock(name="claude_sdk", spec=[])
    sdk.name = "claude_sdk"
    sdk.list_sessions = AsyncMock(return_value=[])

    oc = MagicMock(name="opencode", spec=[])
    oc.name = "opencode"
    oc.list_sessions = AsyncMock(return_value=[])

    def _by_name(name: str) -> Any:
        return {"claude_sdk": sdk, "opencode": oc}[name]

    monkeypatch.setattr(cm, "get_backend_by_name", _by_name)
    monkeypatch.setattr(cm, "get_backend", lambda _cfg: sdk)

    ctx = _FakeCtx(backend="opencode")
    scope = ChatScope(chat_id=1)
    db = MagicMock()

    text, keyboard = await _build_resume_page(
        "ctx", ctx, db, scope, page=0,
    )

    # OpenCode's list_sessions runs; the SDK one is not touched.
    oc.list_sessions.assert_awaited_once()
    sdk.list_sessions.assert_not_called()
    # Empty list -> the "no sessions" copy is returned.
    assert "No sessions found" in text
    assert keyboard is None


async def test_resume_lists_from_default_when_no_context_override(
    monkeypatch: pytest.MonkeyPatch,
):
    """No context-level backend → falls back to the top-level default."""
    sdk = MagicMock(name="claude_sdk", spec=[])
    sdk.name = "claude_sdk"
    sdk.list_sessions = AsyncMock(return_value=[])
    oc = MagicMock(name="opencode", spec=[])
    oc.list_sessions = AsyncMock(return_value=[])

    monkeypatch.setattr(
        cm,
        "get_backend_by_name",
        lambda name: {"claude_sdk": sdk, "opencode": oc}[name],
    )
    monkeypatch.setattr(cm, "get_backend", lambda _cfg: sdk)

    ctx = _FakeCtx(backend=None)
    scope = ChatScope(chat_id=2)
    db = MagicMock()

    await _build_resume_page("ctx", ctx, db, scope, page=0)

    sdk.list_sessions.assert_awaited_once()
    oc.list_sessions.assert_not_called()
