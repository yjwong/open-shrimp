"""Resolution path for ``client_manager.resolve_backend``.

The signature is ``resolve_backend(backend=None, *, scope=None, context=None)``.
Verify each priority arm:

* explicit ``backend`` wins;
* ``context`` (with or without an override) picks the named backend, falling
  back to the top-level default;
* ``scope`` reads the live session's pinned backend;
* no args -> top-level default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

import open_shrimp.client_manager as cm
from open_shrimp.db import ChatScope


@dataclass
class _StubContext:
    """Stand-in for ``ContextConfig`` with just the field we exercise."""

    backend: str | None = None


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch: pytest.MonkeyPatch):
    """Reset module-level state so tests can install their own stubs."""
    monkeypatch.setattr(cm, "_default_backend", None, raising=False)
    cm._active_sessions.clear()
    yield
    cm._active_sessions.clear()


def _install_stub_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the factory cache so resolution uses lightweight stubs."""
    stubs = {
        "claude_sdk": MagicMock(name="claude_sdk", spec=[]),
        "opencode": MagicMock(name="opencode", spec=[]),
    }
    stubs["claude_sdk"].name = "claude_sdk"
    stubs["opencode"].name = "opencode"

    def _get_by_name(name: str) -> Any:
        return stubs[name]

    monkeypatch.setattr(cm, "get_backend_by_name", _get_by_name)
    # The top-level default uses the dict-based factory path; route it too.
    monkeypatch.setattr(
        cm, "get_backend", lambda _cfg: stubs["claude_sdk"],
    )
    return stubs


def test_explicit_backend_short_circuits(monkeypatch: pytest.MonkeyPatch):
    stubs = _install_stub_backends(monkeypatch)
    ctx = _StubContext(backend="opencode")
    # Even though context says opencode, the explicit override wins.
    assert cm.resolve_backend(stubs["claude_sdk"], context=ctx) is stubs["claude_sdk"]


def test_context_with_override_picks_named_backend(
    monkeypatch: pytest.MonkeyPatch,
):
    stubs = _install_stub_backends(monkeypatch)
    ctx = _StubContext(backend="opencode")
    assert cm.resolve_backend(context=ctx) is stubs["opencode"]


def test_context_without_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
):
    stubs = _install_stub_backends(monkeypatch)
    ctx = _StubContext(backend=None)
    assert cm.resolve_backend(context=ctx) is stubs["claude_sdk"]


def test_scope_with_live_session_uses_session_backend(
    monkeypatch: pytest.MonkeyPatch,
):
    stubs = _install_stub_backends(monkeypatch)
    scope = ChatScope(chat_id=1, thread_id=None)
    # Install a fake live session whose backend is opencode.
    cm._active_sessions[scope] = cm.AgentSession(
        client=MagicMock(),
        context_name="ctx",
        backend=stubs["opencode"],
    )
    assert cm.resolve_backend(scope=scope) is stubs["opencode"]


def test_scope_without_live_session_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
):
    stubs = _install_stub_backends(monkeypatch)
    scope = ChatScope(chat_id=42, thread_id=None)
    assert cm.resolve_backend(scope=scope) is stubs["claude_sdk"]


def test_no_args_returns_default(monkeypatch: pytest.MonkeyPatch):
    stubs = _install_stub_backends(monkeypatch)
    assert cm.resolve_backend() is stubs["claude_sdk"]


def test_legacy_positional_backend_compatible(
    monkeypatch: pytest.MonkeyPatch,
):
    """The old single-arg call site (``resolve_backend(None)``) still works."""
    stubs = _install_stub_backends(monkeypatch)
    assert cm.resolve_backend(None) is stubs["claude_sdk"]


def test_pre_scope_callers_get_top_level_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """No scope, no context: the top-level default backend serves the call.

    This is the path scheduler init, bot startup, and ``mcp_proxy``
    construction take — they never serve a specific context's traffic.
    """
    stubs = _install_stub_backends(monkeypatch)
    # Ensure a scope-less call (the scheduler init pattern) hits the default
    # even when active sessions exist for unrelated scopes.
    cm._active_sessions[ChatScope(chat_id=99)] = cm.AgentSession(
        client=MagicMock(),
        context_name="unrelated",
        backend=stubs["opencode"],
    )
    assert cm.resolve_backend() is stubs["claude_sdk"]
