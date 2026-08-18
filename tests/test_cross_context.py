"""Tests for the cross-context query tool (``ask_context``).

Covers gating, the dynamic description, the unknown-target error path,
self-targeting (valid, but new-topic handoff only), and a full happy-path
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
    SandboxConfig,
    TelegramConfig,
)
from open_shrimp.cross_context import _OuterApproval, build_ask_context_tool
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


def test_ask_context_registered_with_single_context() -> None:
    # A single-context config still registers the tool: the current context
    # is a valid target for the new-topic handoff (fork a parallel topic).
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
    assert tool is not None
    assert "only (current context — new-topic handoff only)" in tool.description


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
    # The current context is listed as handoff-only, without its description.
    assert "default (current context — new-topic handoff only)" in tool.description
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
async def test_self_target_inline_outcome_fails_closed(monkeypatch) -> None:
    # The card offers no inline button for a self-target; if the outcome
    # plumbing ever produces "inline" anyway, the sub-query must not run.
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
    )
    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = AssertionError(
        "self-target must never run an inline sub-query",
    )
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )

    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler({"context": "default", "question": "hi"})
    assert result.get("is_error") is True
    assert "new-topic handoff" in result["content"][0]["text"]
    fake_backend.make_client.assert_not_called()


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


class _FakeSandbox:
    def __init__(self) -> None:
        self.started = False

    @property
    def host_address(self) -> str:
        return "host.docker.internal"

    def ensure_environment(self, *, log_file=None) -> None:
        pass

    def ensure_running(self, *, log_file=None) -> None:
        pass

    def provision_workspace(self, *, log_file=None) -> None:
        pass

    def start_agent(self, runtime):
        self.started = True
        return SimpleNamespace(
            cli_path="/tmp/ask-context-wrapper",
            endpoint=None,
            cleanup_paths=[],
        )


class _FakeSandboxManager:
    def __init__(self) -> None:
        self.sandbox = _FakeSandbox()

    def agent_home_dir(self, context_name: str):
        from pathlib import Path

        return Path("/tmp") / f"agent-home-{context_name}"

    def create_sandbox(self, context_name, context, *, runtime):
        self.context_name = context_name
        self.context = context
        self.runtime = runtime
        return self.sandbox


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
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
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
async def test_outer_denial_errors_without_running(monkeypatch) -> None:
    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = AssertionError(
        "sub-query must not run when the outer approval is denied",
    )
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="deny")),
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()

    tool = build_ask_context_tool(
        bot=bot, chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler(
        {"context": "glints-delta-etl", "question": "row count?"},
    )

    assert result.get("is_error") is True
    assert "denied" in result["content"][0]["text"]
    fake_backend.make_client.assert_not_called()


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
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
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
async def test_sandboxed_target_uses_sandbox_launch(monkeypatch) -> None:
    cfg = _config()
    cfg.contexts["glints-delta-etl"].sandbox = SandboxConfig(backend="docker")
    captured = {}

    def _make_client(options):
        captured["cli_path"] = options.cli_path
        captured["allowed"] = list(options.allowed_tools or [])
        return _FakeClient("inside sandbox")

    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = _make_client
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.make_runtime.return_value = SimpleNamespace(name="fake-runtime")
    fake_backend.policy = MagicMock()
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()
    manager = _FakeSandboxManager()

    tool = build_ask_context_tool(
        bot=bot,
        chat_id=1,
        thread_id=None,
        config=cfg,
        context_name="default",
        sandbox_managers={"docker": manager},
    )
    assert tool is not None
    result = await tool.handler({"context": "glints-delta-etl", "question": "q"})

    assert result.get("is_error") is None
    assert manager.sandbox.started is True
    assert captured["cli_path"] == "/tmp/ask-context-wrapper"
    assert "Bash" in captured["allowed"]


@pytest.mark.asyncio
async def test_sandboxed_target_injects_proxied_mcp_servers(monkeypatch) -> None:
    from open_shrimp.mcp_proxy.types import HttpServerConfig, StdioServerConfig

    cfg = _config()
    cfg.contexts["glints-delta-etl"].sandbox = SandboxConfig(backend="docker")
    captured = {}

    def _make_client(options):
        captured["mcp_servers"] = options.mcp_servers
        return _FakeClient("inside sandbox")

    mcp_source = MagicMock()
    mcp_source.stdio_servers.return_value = {
        "db": StdioServerConfig(command="db-mcp", args=[], env={}),
    }
    mcp_source.http_servers.return_value = {
        "figma": HttpServerConfig(url="https://figma", transport="sse", headers={}),
    }

    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = _make_client
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.make_runtime.return_value = SimpleNamespace(name="fake-runtime")
    fake_backend.mcp_config_source.return_value = mcp_source
    fake_backend.policy = MagicMock()
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
    )

    mcp_proxy = MagicMock()
    mcp_proxy.register_context.return_value = "tok123"
    mcp_proxy.get_proxy_url.side_effect = (
        lambda ctx, name, ip: f"http://{ip}/mcp/{ctx}/{name}"
    )
    mcp_proxy.get_http_proxy_url.side_effect = (
        lambda ctx, name, ip: f"http://{ip}/http/{ctx}/{name}"
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()
    manager = _FakeSandboxManager()

    tool = build_ask_context_tool(
        bot=bot,
        chat_id=1,
        thread_id=None,
        config=cfg,
        context_name="default",
        sandbox_managers={"docker": manager},
        mcp_proxy=mcp_proxy,
    )
    assert tool is not None
    result = await tool.handler({"context": "glints-delta-etl", "question": "q"})

    assert result.get("is_error") is None
    mcp_proxy.register_context.assert_called_once()
    servers = captured["mcp_servers"]
    assert servers is not None
    assert servers["db"]["url"] == "http://host.docker.internal/mcp/glints-delta-etl/db"
    assert servers["db"]["headers"]["Authorization"] == "Bearer tok123"
    assert servers["figma"]["type"] == "sse"
    assert (
        servers["figma"]["url"]
        == "http://host.docker.internal/http/glints-delta-etl/figma"
    )


@pytest.mark.asyncio
async def test_sandboxed_target_without_manager_fails_closed(monkeypatch) -> None:
    cfg = _config()
    cfg.contexts["glints-delta-etl"].sandbox = SandboxConfig(backend="docker")

    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = AssertionError(
        "must not create a host client for sandboxed ask_context target",
    )
    fake_backend.make_can_use_tool.return_value = AsyncMock()
    fake_backend.policy = MagicMock()
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock()

    tool = build_ask_context_tool(
        bot=bot, chat_id=1, thread_id=None, config=cfg,
        context_name="default",
    )
    assert tool is not None
    result = await tool.handler({"context": "glints-delta-etl", "question": "q"})

    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "Refusing to run ask_context outside the sandbox" in text
    fake_backend.make_client.assert_not_called()


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
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(outcome="inline")),
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


# --- New-topic handoff ------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_creates_topic_binds_and_dispatches(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(
            outcome="new_topic", message_id=7,
        )),
    )
    # The sub-query path must never run for a handoff.
    fake_backend = MagicMock()
    fake_backend.make_client.side_effect = AssertionError(
        "handoff must not run the inline sub-query",
    )
    monkeypatch.setattr(
        "open_shrimp.client_manager.resolve_backend",
        lambda **kwargs: fake_backend,
    )

    set_calls: list[tuple] = []

    async def _fake_set_active_context(db, scope, name):
        set_calls.append((scope.chat_id, scope.thread_id, name))

    dispatched: list[tuple] = []

    async def _fake_dispatch(prompt, chat_id, thread_id=None, *, placeholder=None):
        # Ordering guarantee: the context must be bound before injection.
        assert set_calls, "context must be bound before dispatch"
        dispatched.append((prompt, chat_id, thread_id, placeholder))

    monkeypatch.setattr(
        "open_shrimp.db.set_active_context", _fake_set_active_context,
    )
    monkeypatch.setattr(
        "open_shrimp.dispatch_registry.dispatch", _fake_dispatch,
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock()
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(is_forum=True))
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=670745),
    )

    tool = build_ask_context_tool(
        bot=bot, chat_id=21491458, thread_id=None, config=_config(),
        context_name="default", db=MagicMock(),
    )
    assert tool is not None
    result = await tool.handler(
        {"context": "glints-delta-etl", "question": "land the durable fix"},
    )

    assert result.get("is_error") is None
    assert "new topic" in result["content"][0]["text"]
    assert "670745" in result["content"][0]["text"]

    bot.create_forum_topic.assert_awaited_once()
    assert set_calls == [(21491458, 670745, "glints-delta-etl")]
    assert len(dispatched) == 1
    prompt, d_chat, d_thread, placeholder = dispatched[0]
    assert (prompt, d_chat, d_thread) == ("land the durable fix", 21491458, 670745)
    # The brief is surfaced as a visible placeholder in the new topic.
    assert placeholder is not None
    assert "land the durable fix" in placeholder
    fake_backend.make_client.assert_not_called()


@pytest.mark.asyncio
async def test_handle_handoff_callback_resolves_future() -> None:
    import asyncio

    from open_shrimp.cross_context import (
        _HANDOFF_TOPIC_PREFIX,
        handle_handoff_callback,
    )
    from open_shrimp.handlers.state import _approval_futures

    data = f"{_HANDOFF_TOPIC_PREFIX}abc123"
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _approval_futures[data] = future

    query = MagicMock()
    query.answer = AsyncMock()
    query.message.text_markdown_v2 = "🔎 *Ask x?*"
    query.message.edit_text = AsyncMock()

    try:
        handled = await handle_handoff_callback(query, data)
    finally:
        _approval_futures.pop(data, None)

    assert handled is True
    assert future.result() == "new_topic"
    query.answer.assert_awaited_once()
    query.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_handoff_callback_ignores_foreign_data() -> None:
    from open_shrimp.cross_context import handle_handoff_callback

    query = MagicMock()
    # A non-handoff callback (e.g. a generic approve) must fall through.
    assert await handle_handoff_callback(query, "approve:xyz") is False


@pytest.mark.asyncio
async def test_handoff_rejected_in_non_forum_chat(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(
            outcome="new_topic", message_id=7,
        )),
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock()
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(is_forum=False))
    bot.create_forum_topic = AsyncMock(
        side_effect=AssertionError("must not create a topic in a non-forum chat"),
    )

    # A non-private chat whose is_forum is falsy must be rejected. (Private
    # chats always allow topic creation and are covered by is_private_chat.)
    tool = build_ask_context_tool(
        bot=bot, chat_id=555, thread_id=None, config=_config(),
        context_name="default", db=MagicMock(), is_private_chat=False,
    )
    assert tool is not None
    result = await tool.handler(
        {"context": "glints-delta-etl", "question": "q"},
    )

    assert result.get("is_error") is True
    assert "forum-enabled chat" in result["content"][0]["text"]
    bot.create_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_allowed_in_private_chat_without_is_forum(monkeypatch) -> None:
    # Regression: a private DM reports is_forum falsy, but bots can always
    # create topics there. The gate must key off is_private_chat, not is_forum.
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(
            outcome="new_topic", message_id=7,
        )),
    )

    async def _noop_set_active_context(db, scope, name):
        pass

    async def _noop_dispatch(prompt, chat_id, thread_id=None, *, placeholder=None):
        pass

    monkeypatch.setattr(
        "open_shrimp.db.set_active_context", _noop_set_active_context,
    )
    monkeypatch.setattr(
        "open_shrimp.dispatch_registry.dispatch", _noop_dispatch,
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock()
    # A private chat: is_forum is None. get_chat must not gate the decision.
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(is_forum=None))
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=42),
    )

    tool = build_ask_context_tool(
        bot=bot, chat_id=21491458, thread_id=None, config=_config(),
        context_name="default", db=MagicMock(), is_private_chat=True,
    )
    assert tool is not None
    result = await tool.handler(
        {"context": "glints-delta-etl", "question": "q"},
    )

    assert result.get("is_error") is None
    bot.create_forum_topic.assert_awaited_once()
    bot.get_chat.assert_not_called()


# --- Self-target (new-topic handoff only) -----------------------------------


@pytest.mark.asyncio
async def test_self_target_handoff_binds_same_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_shrimp.cross_context._request_outer_approval",
        AsyncMock(return_value=_OuterApproval(
            outcome="new_topic", message_id=7,
        )),
    )

    set_calls: list[tuple] = []

    async def _fake_set_active_context(db, scope, name):
        set_calls.append((scope.chat_id, scope.thread_id, name))

    dispatched: list[tuple] = []

    async def _fake_dispatch(prompt, chat_id, thread_id=None, *, placeholder=None):
        dispatched.append((prompt, chat_id, thread_id))

    monkeypatch.setattr(
        "open_shrimp.db.set_active_context", _fake_set_active_context,
    )
    monkeypatch.setattr(
        "open_shrimp.dispatch_registry.dispatch", _fake_dispatch,
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock()
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=99),
    )

    tool = build_ask_context_tool(
        bot=bot, chat_id=1, thread_id=None, config=_config(),
        context_name="default", db=MagicMock(),
    )
    assert tool is not None
    result = await tool.handler(
        {"context": "default", "question": "fork this subtask"},
    )

    assert result.get("is_error") is None
    assert "new topic" in result["content"][0]["text"]
    # The new topic runs under the SAME context as the caller.
    assert set_calls == [(1, 99, "default")]
    assert dispatched == [("fork this subtask", 1, 99)]

    # The approval card was requested without the inline option.
    from open_shrimp import cross_context

    approval_mock = cross_context._request_outer_approval
    assert approval_mock.await_args.kwargs["include_inline"] is False


@pytest.mark.asyncio
async def test_outer_approval_card_omits_inline_for_self() -> None:
    import asyncio

    from open_shrimp.cross_context import (
        _HANDOFF_INLINE_PREFIX,
        _request_outer_approval,
    )
    from open_shrimp.handlers.state import _approval_futures

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))

    task = asyncio.create_task(_request_outer_approval(
        bot=bot, chat_id=1, thread_id=None, target="default",
        question="q", include_inline=False,
    ))
    while not bot.send_message.await_count:
        await asyncio.sleep(0)

    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert labels == ["New topic", "Deny"]
    # No future is registered under the (unused) inline callback data.
    assert not any(
        k.startswith(_HANDOFF_INLINE_PREFIX) for k in _approval_futures
    )

    topic_data = keyboard.inline_keyboard[0][0].callback_data
    _approval_futures[topic_data].set_result("new_topic")
    approval = await task
    assert approval.outcome == "new_topic"
    assert approval.message_id == 9


@pytest.mark.asyncio
async def test_unanswered_card_does_not_hold_the_concurrency_slot(
    monkeypatch,
) -> None:
    """A card awaiting a tap must not wedge other contexts' queries.

    With the slot held across the approval, a later call blocks before it can
    post a card of its own — leaving the user nothing to tap to clear it.
    """
    import asyncio

    from open_shrimp import cross_context

    monkeypatch.setattr(cross_context, "_semaphore", asyncio.Semaphore(1))

    reached = asyncio.Event()
    arrivals = 0
    release = asyncio.Event()

    async def _blocking_approval(**kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals >= 2:
            reached.set()
        await release.wait()
        return _OuterApproval(outcome="deny")

    monkeypatch.setattr(
        cross_context, "_request_outer_approval", _blocking_approval,
    )

    tool = build_ask_context_tool(
        bot=MagicMock(), chat_id=1, thread_id=None, config=_config(),
        context_name="default",
    )
    assert tool is not None
    args = {"context": "glints-delta-etl", "question": "q"}
    tasks = [
        asyncio.create_task(tool.handler(dict(args))) for _ in range(2)
    ]
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)

    assert arrivals == 2
    assert all(r.get("is_error") for r in results)


@pytest.mark.asyncio
async def test_outer_approval_registers_pending_card_for_scope() -> None:
    """The card is discoverable per-scope while it waits, and only while."""
    import asyncio

    from open_shrimp.cross_context import _request_outer_approval
    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import _approval_futures, has_pending_approval

    scope = ChatScope(chat_id=1, thread_id=77)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))

    assert has_pending_approval(scope) is False
    task = asyncio.create_task(_request_outer_approval(
        bot=bot, chat_id=1, thread_id=77, target="default", question="q",
    ))
    while not bot.send_message.await_count:
        await asyncio.sleep(0)

    assert has_pending_approval(scope) is True

    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    deny_data = keyboard.inline_keyboard[0][-1].callback_data
    _approval_futures[deny_data].set_result("deny")
    await task

    assert has_pending_approval(scope) is False
