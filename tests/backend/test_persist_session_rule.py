"""Round-trip tests for ``BackendPolicy.persist_session_rule`` /
``BackendPolicy.load_persistent_rules``.

Durable Bash prefix rules go through the active backend's policy:

* ``ClaudeSdkPolicy`` writes / reads ``.claude/settings.local.json`` under
  the context's working directory.
* ``OpenCodePolicy`` patches the live ``OpenCodeClient`` via
  ``patch_config_permission`` (and ``load_persistent_rules`` returns
  ``[]`` because durable rules arrive via the ``permission.asked.always``
  event arm).

These tests pin both sides of the round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.opencode.policy import OpenCodePolicy
from open_shrimp.db import ChatScope
from open_shrimp.hooks import ApprovalRule


# ---------------------------------------------------------------------------
# SDK round-trip: .claude/settings.local.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_persist_then_load_bash_prefix_rule(tmp_path: Path) -> None:
    """Persisting a Bash prefix rule writes settings.local.json; loading
    re-reads the same rule back."""
    policy = ClaudeSdkPolicy()
    scope = ChatScope(chat_id=1, thread_id=None)
    rule = ApprovalRule(tool_name="Bash", pattern="git *")

    persisted = await policy.persist_session_rule(
        rule, directory=str(tmp_path), scope=scope,
    )
    assert persisted is True

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == ["Bash(git:*)"]

    loaded = await policy.load_persistent_rules(directory=str(tmp_path))
    assert loaded == [ApprovalRule(tool_name="Bash", pattern="git *")]


@pytest.mark.asyncio
async def test_sdk_persist_returns_false_when_already_present(
    tmp_path: Path,
) -> None:
    """Persisting the same rule twice is a no-op on the second call."""
    policy = ClaudeSdkPolicy()
    scope = ChatScope(chat_id=1)
    rule = ApprovalRule(tool_name="Bash", pattern="npm *")

    assert await policy.persist_session_rule(
        rule, directory=str(tmp_path), scope=scope,
    ) is True
    assert await policy.persist_session_rule(
        rule, directory=str(tmp_path), scope=scope,
    ) is False


@pytest.mark.asyncio
async def test_sdk_load_empty_for_fresh_directory(tmp_path: Path) -> None:
    """``load_persistent_rules`` returns ``[]`` when no settings file exists."""
    policy = ClaudeSdkPolicy()
    loaded = await policy.load_persistent_rules(directory=str(tmp_path))
    assert loaded == []


# ---------------------------------------------------------------------------
# OpenCode: patch_config_permission via the live client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencode_persist_calls_patch_config_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode's ``persist_session_rule`` looks up the live client for
    *scope* and forwards a ``{permission: {pattern: "allow"}}`` payload."""
    policy = OpenCodePolicy()
    scope = ChatScope(chat_id=42, thread_id=None)
    rule = ApprovalRule(tool_name="Bash", pattern="git *")

    client = MagicMock()
    client.patch_config_permission = AsyncMock(return_value=None)
    session = MagicMock()
    session.client = client

    monkeypatch.setattr(
        "open_shrimp.client_manager.get_session",
        lambda _scope: session,
    )

    persisted = await policy.persist_session_rule(
        rule, directory="/unused", scope=scope,
    )
    assert persisted is True
    client.patch_config_permission.assert_awaited_once_with(
        {"bash": {"git *": "allow"}},
    )


@pytest.mark.asyncio
async def test_opencode_persist_returns_false_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No active session means we can't reach the live client — return False."""
    policy = OpenCodePolicy()
    monkeypatch.setattr(
        "open_shrimp.client_manager.get_session",
        lambda _scope: None,
    )
    persisted = await policy.persist_session_rule(
        ApprovalRule(tool_name="Bash", pattern="git *"),
        directory="/unused",
        scope=ChatScope(chat_id=1),
    )
    assert persisted is False


@pytest.mark.asyncio
async def test_opencode_persist_returns_false_when_client_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed HTTP patch surfaces as ``False``, not an exception."""
    policy = OpenCodePolicy()
    client = MagicMock()
    client.patch_config_permission = AsyncMock(side_effect=RuntimeError("boom"))
    session = MagicMock()
    session.client = client
    monkeypatch.setattr(
        "open_shrimp.client_manager.get_session",
        lambda _scope: session,
    )
    persisted = await policy.persist_session_rule(
        ApprovalRule(tool_name="Bash", pattern="git *"),
        directory="/unused",
        scope=ChatScope(chat_id=1),
    )
    assert persisted is False


@pytest.mark.asyncio
async def test_opencode_persist_returns_false_for_blanket_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blanket (pattern=None) rule has no OpenCode-side translation
    in this PR — the policy declines and the caller falls back to the
    in-memory cache only."""
    policy = OpenCodePolicy()
    client = MagicMock()
    client.patch_config_permission = AsyncMock()
    session = MagicMock()
    session.client = client
    monkeypatch.setattr(
        "open_shrimp.client_manager.get_session",
        lambda _scope: session,
    )
    persisted = await policy.persist_session_rule(
        ApprovalRule(tool_name="Bash", pattern=None),
        directory="/unused",
        scope=ChatScope(chat_id=1),
    )
    assert persisted is False
    client.patch_config_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_opencode_load_returns_empty(tmp_path: Path) -> None:
    """OpenCode durable rules arrive through the ``permission.asked.always``
    event arm, so ``load_persistent_rules`` is intentionally a no-op stub
    at this layer."""
    policy = OpenCodePolicy()
    loaded = await policy.load_persistent_rules(directory=str(tmp_path))
    assert loaded == []
