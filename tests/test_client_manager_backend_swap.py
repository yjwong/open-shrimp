"""When the resolved backend for a scope changes, the live client must be
torn down and rebuilt against the new backend.  The persisted mapping is
keyed by backend, so the resume id reaching ``get_or_create_session`` is
already the incoming backend's and the rebuild carries it through.

Covers both the same-context-but-backend-edited path and the
``/context`` cross-backend swap, which share the close+reopen body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.rich_stub import rich_sends

import open_shrimp.client_manager as cm
from open_shrimp.db import ChatScope

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeCtx:
    """Stand-in for ``ContextConfig`` with the fields ``get_or_create_session``
    touches before bailing out."""

    directory: str = "/tmp/openshrimp-fake"
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    additional_directories: list[str] = field(default_factory=list)
    default_for_chats: list[int] = field(default_factory=list)
    locked_for_chats: list[int] = field(default_factory=list)
    container: Any = None
    sandbox: Any = None
    mcp: dict[str, Any] = field(default_factory=dict)
    backend: str | None = None


def _make_backend(name: str) -> Any:
    backend = MagicMock(name=f"backend_{name}", spec=[])
    backend.name = name
    backend.policy = MagicMock(name=f"policy_{name}", spec=[])
    backend.policy.auto_approved_at_session_start = MagicMock(return_value=[])
    backend.make_can_use_tool = MagicMock(return_value=MagicMock())
    # Nothing to fetch on the host, so no first-turn download message.
    backend.host_prefetch = MagicMock(return_value=None)
    client = MagicMock(name=f"client_{name}", spec=[])
    client.is_alive = MagicMock(return_value=True)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.session_id = None
    backend.make_client = MagicMock(return_value=client)
    backend.mcp_config_source = MagicMock(
        return_value=MagicMock(
            stdio_servers=lambda _ctx: {},
            http_servers=lambda _ctx: {},
        ),
    )
    backend.make_tool_server = MagicMock()
    return backend


def _sandboxed_session(runtime_name: str) -> Any:
    """A live sandboxed session whose credential target is ``(runtime, ctx)``."""
    client = MagicMock(spec=[])
    client.disconnect = AsyncMock()
    return cm.AgentSession(
        client=client,
        context_name="ctx",
        sandbox=MagicMock(name="sandbox", spec=[]),
        runtime=SimpleNamespace(name=runtime_name),
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    cm._active_sessions.clear()
    monkeypatch.setattr(cm, "_default_backend", None, raising=False)
    yield
    cm._active_sessions.clear()


@pytest.fixture
def unregistered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Collects the ``(runtime, context)`` pairs ``close_session`` retires."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cm, "unregister_cred_sandbox",
        lambda runtime, ctx: calls.append((runtime, ctx)),
    )
    return calls


async def test_close_session_disconnects_and_evicts():
    """``close_session`` disconnects the live client and drops the scope.

    Every teardown path (``/clear``, context switch, backend swap, shutdown)
    funnels through here; leaving the entry behind would hand a later turn a
    client whose transport is already gone.
    """
    sdk = _make_backend("claude_sdk")

    scope = ChatScope(chat_id=1, thread_id=None)
    old_client = MagicMock(spec=[])
    old_client.is_alive = MagicMock(return_value=True)
    old_client.disconnect = AsyncMock()

    cm._active_sessions[scope] = cm.AgentSession(
        client=old_client,
        session_id="old-session-id",
        context_name="ctx",
        backend=sdk,
    )

    await cm.close_session(scope)

    old_client.disconnect.assert_awaited_once()
    assert scope not in cm._active_sessions


async def test_close_session_unregisters_only_its_own_credential_target(
    unregistered: list[tuple[str, str]],
):
    """Two topics on one context can run different runtimes in one guest.

    The credential watcher is keyed by ``(runtime, context)``, so closing the
    Claude topic must retire the Claude target even though an OpenCode session
    on the same context is still live — a context-only check would leave it
    registered for a home nothing writes to any more.
    """
    claude_scope = ChatScope(chat_id=3, thread_id=11)
    opencode_scope = ChatScope(chat_id=3, thread_id=22)
    cm._active_sessions[claude_scope] = _sandboxed_session("claude")
    cm._active_sessions[opencode_scope] = _sandboxed_session("opencode")

    await cm.close_session(claude_scope)

    assert unregistered == [("claude", "ctx")]


async def test_close_session_keeps_a_target_a_sibling_topic_still_uses(
    unregistered: list[tuple[str, str]],
):
    first = ChatScope(chat_id=4, thread_id=11)
    second = ChatScope(chat_id=4, thread_id=22)
    cm._active_sessions[first] = _sandboxed_session("claude")
    cm._active_sessions[second] = _sandboxed_session("claude")

    await cm.close_session(first)

    assert unregistered == []


async def test_backend_swap_resumes_the_incoming_backends_session(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live swap rebuilds on the resume id the caller looked up.

    That lookup is already keyed by the incoming backend, so the id here is
    OpenCode's own — the swap must hand it to the new client rather than
    discarding it, or switching back and forth would lose both histories.
    """
    sdk = _make_backend("claude_sdk")
    oc = _make_backend("opencode")
    monkeypatch.setattr(
        cm,
        "get_backend_by_name",
        lambda name: {"claude_sdk": sdk, "opencode": oc}[name],
    )

    scope = ChatScope(chat_id=7, thread_id=None)
    old_client = MagicMock(spec=[])
    old_client.is_alive = MagicMock(return_value=True)
    old_client.disconnect = AsyncMock()
    cm._active_sessions[scope] = cm.AgentSession(
        client=old_client,
        session_id="claude-session-id",
        context_name="ctx",
        backend=sdk,
    )

    ctx = _FakeCtx(backend="opencode")
    cb = MagicMock(spec=[])

    session = await cm.get_or_create_session(
        scope=scope,
        context_name="ctx",
        context=ctx,
        session_id="opencode-session-id",
        callback_context=cb,
        db=MagicMock(name="db"),
    )

    old_client.disconnect.assert_awaited_once()
    assert session.session_id == "opencode-session-id"
    assert session.backend is oc


async def test_backend_swap_notifies_user(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live backend swap tells the user the conversation moved.

    The incoming backend answers from its own session, so the topic reads
    as a different conversation; the notice explains why.
    """
    sdk = _make_backend("claude_sdk")
    oc = _make_backend("opencode")
    monkeypatch.setattr(
        cm,
        "get_backend_by_name",
        lambda name: {"claude_sdk": sdk, "opencode": oc}[name],
    )

    scope = ChatScope(chat_id=9, thread_id=None)
    old_client = MagicMock(spec=[])
    old_client.is_alive = MagicMock(return_value=True)
    old_client.disconnect = AsyncMock()
    cm._active_sessions[scope] = cm.AgentSession(
        client=old_client,
        session_id="old-session-id",
        context_name="ctx",
        backend=sdk,
    )

    ctx = _FakeCtx(backend="opencode")
    cb = MagicMock(spec=[])
    bot = MagicMock(name="bot", spec=["do_api_request"])
    bot.send_message = AsyncMock()

    await cm.get_or_create_session(
        scope=scope,
        context_name="ctx",
        context=ctx,
        session_id="old-session-id",
        callback_context=cb,
        db=MagicMock(name="db"),
        bot=bot,
    )

    swap_notices = [
        call for call in rich_sends(bot) if "Backend changed" in call.text
    ]
    assert len(swap_notices) == 1
    assert "claude_sdk" in swap_notices[0].text
    assert "opencode" in swap_notices[0].text
    assert swap_notices[0].chat_id == 9
