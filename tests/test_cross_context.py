"""Tests for the cross-context query tool (``ask_context``).

Covers gating (registered only when another context exists), the dynamic
description, the unknown/self target error paths, and a full happy-path
sub-query run with a faked backend client.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_shrimp.backend.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)
from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.cross_context import build_ask_context_tool
from open_shrimp.tools import create_openshrimp_tools


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp/default",
                description="personal default context",
                allowed_tools=[],
            ),
            "glints-delta-etl": ContextConfig(
                directory="/tmp/etl",
                description="the ETL pipeline project",
                allowed_tools=["mcp__etldb__run_query"],
            ),
        },
        default_context="default",
        review=ReviewConfig(),
    )


# --- Gating -----------------------------------------------------------------


def test_ask_context_registered_when_other_context_exists() -> None:
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, config=_config(), context_name="default",
    )
    names = [t.name for t in tools]
    assert "ask_context" in names


def test_ask_context_absent_without_context_name() -> None:
    # No context_name -> cannot exclude self / guard recursion -> not built.
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, config=_config(),
    )
    assert "ask_context" not in [t.name for t in tools]


def test_ask_context_absent_when_no_other_context() -> None:
    cfg = Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "only": ContextConfig(
                directory="/tmp", description="d", allowed_tools=[],
            ),
        },
        default_context="only",
        review=ReviewConfig(),
    )
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=cfg,
        context_name="only",
    )
    assert tool is None


def test_ask_context_survives_partial_config() -> None:
    # Mirrors the mcp_proxy tests that pass a SimpleNamespace stand-in.
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None,
        config=SimpleNamespace(default_context="default"),
        context_name="default",
    )
    assert tool is None


def test_description_lists_other_contexts() -> None:
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    assert "glints-delta-etl" in tool.description
    assert "the ETL pipeline project" in tool.description
    # The current context is excluded from the list.
    assert "personal default context" not in tool.description
    assert tool.read_only is False


# --- Error paths ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_target_errors() -> None:
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler({"context": "nope", "question": "hi"})
    assert result.get("is_error") is True
    assert "no queryable context" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_self_target_errors() -> None:
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler({"context": "default", "question": "hi"})
    assert result.get("is_error") is True
    assert "current context" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_missing_question_errors() -> None:
    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler({"context": "glints-delta-etl", "question": ""})
    assert result.get("is_error") is True


# --- Happy path -------------------------------------------------------------


class _FakeClient:
    """A minimal BackendClient that yields a canned answer."""

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text=self._answer)])
        yield ResultMessage(session_id="sub-session")

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_happy_path_returns_answer(monkeypatch) -> None:
    fake_client = _FakeClient("~2.4M rows in staging.")
    fake_backend = MagicMock()
    fake_backend.make_client.return_value = fake_client
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.policy = MagicMock()

    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )

    bot = MagicMock()
    sent = SimpleNamespace(message_id=42)
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()

    tool = build_ask_context_tool(
        bot=bot, chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None

    result = await tool.handler(
        {"context": "glints-delta-etl", "question": "row count?"},
    )

    assert result.get("is_error") is None
    text = result["content"][0]["text"]
    assert text.startswith("[glints-delta-etl answered]")
    assert "~2.4M rows in staging." in text

    # Sub-query lifecycle ran to completion.
    assert fake_client.connected is True
    assert fake_client.disconnected is True

    # Status message posted then edited to a success summary.
    bot.send_message.assert_awaited()
    bot.edit_message_text.assert_awaited()
    final_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "answered" in final_text


@pytest.mark.asyncio
async def test_allowed_tools_inherit_target(monkeypatch) -> None:
    captured = {}

    def _make_client(options):
        captured["allowed"] = list(options.allowed_tools or [])
        return _FakeClient("ok")

    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = _make_client
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.policy = MagicMock()

    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )
    # Treat the target as non-sandboxed so Bash is not added.
    monkeypatch.setattr("open_shrimp.config.is_sandboxed", lambda ctx: False)

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()

    tool = build_ask_context_tool(
        bot=bot, chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    await tool.handler({"context": "glints-delta-etl", "question": "q"})

    # Base read tools plus the target's own trusted MCP tool; no Bash.
    assert "Read" in captured["allowed"]
    assert "mcp__etldb__run_query" in captured["allowed"]
    assert "Bash" not in captured["allowed"]


@pytest.mark.asyncio
async def test_transient_task_unregistered_after_run(monkeypatch) -> None:
    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import _active_bg_tasks

    fake_backend = MagicMock()
    fake_backend.make_client.return_value = _FakeClient("done")
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.policy = MagicMock()
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()

    tool = build_ask_context_tool(
        bot=bot, chat_id=55, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    await tool.handler({"context": "glints-delta-etl", "question": "q"})

    # The sink registers a transient task during the run and must clean it
    # up afterwards, leaving no orphan scope entry.
    assert ChatScope(chat_id=55, thread_id=None) not in _active_bg_tasks


def test_register_unregister_transient_task_owner() -> None:
    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import (
        _active_bg_tasks,
        is_task_active,
        register_transient_task,
        unregister_transient_task,
    )

    scope = ChatScope(chat_id=999, thread_id=7)
    register_transient_task(
        scope, "tid1", description="d", task_type="ask_context",
    )
    assert is_task_active("tid1") is True

    # Cleanup drops the scope entry entirely once empty.
    unregister_transient_task(scope, "tid1")
    assert is_task_active("tid1") is False
    assert scope not in _active_bg_tasks

    # Idempotent / safe on unknown ids.
    unregister_transient_task(scope, "tid1")
