"""When the resolved backend for a scope changes, the live client must be
torn down and rebuilt against the new backend.  Session IDs are
backend-scoped, so the old session id is dropped on the rebuild.

Covers both the same-context-but-backend-edited path and the
``/context`` cross-backend swap, which share the close+reopen body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    cm._active_sessions.clear()
    monkeypatch.setattr(cm, "_default_backend", None, raising=False)
    yield
    cm._active_sessions.clear()


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


async def test_backend_swap_clears_persisted_session(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live backend swap must also drop the persisted session mapping.

    The stored id belongs to the old backend; leaving it would make a later
    cold start try to resume a foreign session (failing over to fresh with a
    spurious warning).  ``get_or_create_session`` clears it via
    ``delete_session`` when it closes the old client.
    """
    sdk = _make_backend("claude_sdk")
    oc = _make_backend("opencode")
    monkeypatch.setattr(
        cm,
        "get_backend_by_name",
        lambda name: {"claude_sdk": sdk, "opencode": oc}[name],
    )
    deleted = AsyncMock()
    monkeypatch.setattr(cm, "delete_session", deleted)

    scope = ChatScope(chat_id=7, thread_id=None)
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
    db = MagicMock(name="db")

    session = await cm.get_or_create_session(
        scope=scope,
        context_name="ctx",
        context=ctx,
        session_id="old-session-id",
        callback_context=cb,
        db=db,
    )

    deleted.assert_awaited_once_with(db, scope, "ctx")
    # The rebuilt session must not carry the old backend's resume id.
    assert session.session_id is None
    assert session.backend is oc


async def test_backend_swap_notifies_user(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live backend swap tells the user the conversation reset.

    Sessions are backend-scoped, so the swap silently drops history; the
    notice explains why the bot appears to have forgotten the conversation.
    """
    sdk = _make_backend("claude_sdk")
    oc = _make_backend("opencode")
    monkeypatch.setattr(
        cm,
        "get_backend_by_name",
        lambda name: {"claude_sdk": sdk, "opencode": oc}[name],
    )
    monkeypatch.setattr(cm, "delete_session", AsyncMock())

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
    bot = MagicMock(name="bot", spec=[])
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
        call for call in bot.send_message.call_args_list
        if "Backend changed" in call.kwargs.get("text", "")
    ]
    assert len(swap_notices) == 1
    text = swap_notices[0].kwargs["text"]
    assert "claude_sdk" in text and "opencode" in text
    assert swap_notices[0].kwargs["chat_id"] == 9
