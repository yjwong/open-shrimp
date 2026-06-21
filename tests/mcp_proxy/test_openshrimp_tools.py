"""End-to-end tests for the OpenShrimp tools HTTP bridge.

These exercise the *real* ``/tools/{scope_token}`` JSON-RPC endpoint via
``httpx.ASGITransport`` (in-process, no socket) — the same path production
uses.  Gating, ``read_only`` parity, dispatch, and error envelopes are all
asserted over HTTP.  Pure handler-internal edge cases stay as direct calls
in ``tests/test_tools.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from open_shrimp.mcp_proxy import McpProxy
from open_shrimp.mcp_proxy.registry import ProxyRegistry
from open_shrimp.mcp_proxy.server import _create_proxy_app
from open_shrimp.mcp_proxy.stdio_manager import StdioManager
from open_shrimp.tools import create_openshrimp_tools

pytestmark = pytest.mark.asyncio


class FakeBot:
    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.topic_edits: list[dict] = []

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)

    async def send_photo(self, **kwargs):
        self.documents.append(kwargs)

    async def get_forum_topic_icon_stickers(self):
        return [SimpleNamespace(emoji="📝", custom_emoji_id="emoji-id")]

    async def edit_forum_topic(self, **kwargs):
        self.topic_edits.append(kwargs)


class FakeSandbox:
    def __init__(self, screenshots_dir) -> None:
        self.screenshots_dir = screenshots_dir
        self.clicked = None

    def get_screenshots_dir(self):
        return self.screenshots_dir

    def supports_port_forwarding(self):
        return False

    def take_screenshot(self, output_path):
        output_path.write_bytes(b"png")

    def send_click(self, x, y, button="left"):
        self.clicked = (x, y, button)


class _StubOAuthProvider:
    def get(self, server_name, server_url):
        return None


async def _client(registry: ProxyRegistry):
    http_client = httpx.AsyncClient()
    app = _create_proxy_app(
        registry, StdioManager(), http_client, _StubOAuthProvider()
    )
    transport = httpx.ASGITransport(app=app)
    return (
        httpx.AsyncClient(transport=transport, base_url="http://testserver"),
        http_client,
    )


async def _rpc(client: httpx.AsyncClient, token: str, method: str, params=None):
    response = await client.post(
        f"/tools/{token}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()["result"]


def _register_tools(
    registry: ProxyRegistry,
    *,
    context_name: str = "default",
    chat_id: int = 1,
    thread_id: int | None = None,
    user_id: int = 10,
    is_private_chat: bool = True,
    bot=None,
    db=None,
    config=None,
    job_queue=None,
    sandbox=None,
    host_bash_workdir: str | None = None,
) -> str:
    bot = bot or FakeBot()

    def tool_factory():
        # NOTE: master's create_openshrimp_tools signature — inference via
        # ``sandbox=`` / ``host_bash_workdir=``, NOT opencode's include_* bools.
        return create_openshrimp_tools(
            bot=bot,
            chat_id=chat_id,
            thread_id=thread_id,
            db=db,
            config=config,
            job_queue=job_queue,
            sandbox=sandbox,
            context_name=context_name,
            user_id=user_id,
            is_private_chat=is_private_chat,
            host_bash_workdir=host_bash_workdir,
        )

    return registry.register_tool_scope(
        context_name=context_name,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        tool_factory=tool_factory,
    )


# --- Gating -----------------------------------------------------------------


async def test_tools_list_private_chat_excludes_edit_topic() -> None:
    registry = ProxyRegistry()
    token = _register_tools(
        registry,
        db=object(),
        config=SimpleNamespace(default_context="default"),
        job_queue=object(),
    )
    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    names = {tool["name"] for tool in result["tools"]}
    assert "send_file" in names
    assert "edit_topic" not in names
    assert {"create_schedule", "list_schedules", "delete_schedule"} <= names


async def test_tools_list_forum_thread_includes_edit_topic() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry, chat_id=7, thread_id=9, is_private_chat=False)
    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    names = {tool["name"] for tool in result["tools"]}
    assert "edit_topic" in names


async def test_scheduling_tools_require_db_config_jobqueue() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry, db=object(), config=SimpleNamespace())
    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    names = {tool["name"] for tool in result["tools"]}
    assert "create_schedule" not in names


async def test_host_bash_only_listed_with_workdir(tmp_path) -> None:
    registry = ProxyRegistry()
    token_without = _register_tools(registry)
    token_with = _register_tools(
        registry, chat_id=2, host_bash_workdir=str(tmp_path),
    )
    client, backing = await _client(registry)
    try:
        without = await _rpc(client, token_without, "tools/list")
        with_ = await _rpc(client, token_with, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    assert "host_bash" not in {t["name"] for t in without["tools"]}
    assert "host_bash" in {t["name"] for t in with_["tools"]}


async def test_computer_tools_listed_with_screenshots_dir(tmp_path) -> None:
    sandbox = FakeSandbox(tmp_path)
    registry = ProxyRegistry()
    token = _register_tools(registry, sandbox=sandbox)
    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    names = {tool["name"] for tool in result["tools"]}
    assert "computer_screenshot" in names
    assert "computer_click" in names


# --- Dispatch + errors ------------------------------------------------------


async def test_edit_topic_dispatches_to_registered_scope() -> None:
    bot = FakeBot()
    registry = ProxyRegistry()
    token = _register_tools(
        registry, chat_id=123, thread_id=456, is_private_chat=False, bot=bot,
    )
    client, backing = await _client(registry)
    try:
        result = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "edit_topic", "arguments": {"title": "New Title", "icon": "📝"}},
        )
    finally:
        await client.aclose()
        await backing.aclose()

    assert result["content"][0]["text"].startswith("Topic updated")
    assert bot.topic_edits == [
        {
            "chat_id": 123,
            "message_thread_id": 456,
            "name": "New Title",
            "icon_custom_emoji_id": "emoji-id",
        }
    ]


async def test_send_file_missing_path_returns_tool_error() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry)
    client, backing = await _client(registry)
    try:
        result = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "send_file", "arguments": {"file_path": "/nope/xyz"}},
        )
    finally:
        await client.aclose()
        await backing.aclose()

    assert result["is_error"] is True
    assert "File not found" in result["content"][0]["text"]


async def test_computer_click_uses_master_message(tmp_path) -> None:
    sandbox = FakeSandbox(tmp_path)
    registry = ProxyRegistry()
    token = _register_tools(registry, sandbox=sandbox)
    client, backing = await _client(registry)
    try:
        result = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "computer_click", "arguments": {"x": 12, "y": 34}},
        )
    finally:
        await client.aclose()
        await backing.aclose()

    # master's message — NOT opencode's "Click sent."
    assert result["content"][0]["text"] == "Clicked left at (12, 34)."
    assert sandbox.clicked == (12, 34, "left")


async def test_unknown_tool_returns_rpc_error() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry)
    client, backing = await _client(registry)
    try:
        response = await client.post(
            f"/tools/{token}",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
        )
    finally:
        await client.aclose()
        await backing.aclose()

    body = response.json()
    assert body["error"]["code"] == -32602
    assert "Unknown tool" in body["error"]["message"]


async def test_handler_exception_wrapped_not_500() -> None:
    """A handler that raises produces an is_error envelope, not HTTP 500."""
    registry = ProxyRegistry()
    # send_file catches its own exceptions, so to test the RPC-layer wrapper
    # we drive edit_topic with a bot whose edit raises.
    class RaisingBot(FakeBot):
        async def edit_forum_topic(self, **kwargs):
            raise RuntimeError("kaboom")

    token = _register_tools(
        registry, chat_id=5, thread_id=6, is_private_chat=False, bot=RaisingBot(),
    )
    client, backing = await _client(registry)
    try:
        response = await client.post(
            f"/tools/{token}",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "edit_topic", "arguments": {"title": "x"}},
            },
        )
    finally:
        await client.aclose()
        await backing.aclose()

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["is_error"] is True


async def test_unknown_scope_token_404() -> None:
    registry = ProxyRegistry()
    client, backing = await _client(registry)
    try:
        response = await client.post(
            "/tools/not-a-real-token",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    finally:
        await client.aclose()
        await backing.aclose()
    assert response.status_code == 404


async def test_get_and_delete_405() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry)
    client, backing = await _client(registry)
    try:
        get_resp = await client.get(f"/tools/{token}")
        del_resp = await client.delete(f"/tools/{token}")
    finally:
        await client.aclose()
        await backing.aclose()
    assert get_resp.status_code == 405
    assert get_resp.headers["Allow"] == "POST"
    assert del_resp.status_code == 405


# --- Prefix + serverInfo + read_only parity ---------------------------------


async def test_tools_list_names_match_factory() -> None:
    registry = ProxyRegistry()
    bot = FakeBot()
    kwargs = dict(
        bot=bot, chat_id=1, thread_id=9, db=object(),
        config=SimpleNamespace(default_context="default"), job_queue=object(),
        user_id=10, is_private_chat=False, host_bash_workdir=None,
    )
    token = _register_tools(
        registry, chat_id=1, thread_id=9, is_private_chat=False, bot=bot,
        db=kwargs["db"], config=kwargs["config"], job_queue=kwargs["job_queue"],
    )
    direct_names = {t.name for t in create_openshrimp_tools(**kwargs)}

    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    http_names = {t["name"] for t in result["tools"]}
    assert http_names == direct_names


async def test_initialize_serverinfo_name() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry)
    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "initialize")
    finally:
        await client.aclose()
        await backing.aclose()
    assert result["serverInfo"]["name"] == "openshrimp"


async def test_read_only_hint_parity() -> None:
    registry = ProxyRegistry()
    bot = FakeBot()
    kwargs = dict(
        bot=bot, chat_id=1, thread_id=9, db=object(),
        config=SimpleNamespace(default_context="default"), job_queue=object(),
        user_id=10, is_private_chat=False, host_bash_workdir="/tmp",
    )
    token = _register_tools(
        registry, chat_id=1, thread_id=9, is_private_chat=False, bot=bot,
        db=kwargs["db"], config=kwargs["config"], job_queue=kwargs["job_queue"],
        host_bash_workdir="/tmp",
    )
    by_name = {t.name: t for t in create_openshrimp_tools(**kwargs)}

    client, backing = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing.aclose()

    for tool in result["tools"]:
        assert (
            tool["annotations"]["readOnlyHint"]
            == by_name[tool["name"]].read_only
        ), tool["name"]


# --- Notification handling --------------------------------------------------


async def test_initialized_notification_returns_202() -> None:
    registry = ProxyRegistry()
    token = _register_tools(registry)
    client, backing = await _client(registry)
    try:
        response = await client.post(
            f"/tools/{token}",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    finally:
        await client.aclose()
        await backing.aclose()
    assert response.status_code == 202


# --- Registry unit ----------------------------------------------------------


# --- Proxy lifecycle (real listener, no sandboxed context) ------------------


async def test_proxy_lifecycle_serves_tools_over_loopback() -> None:
    """The always-on listener serves tools/list over the bound 127.0.0.1 port."""
    proxy = McpProxy(_StubOAuthProvider())
    await proxy.start()
    try:
        bot = FakeBot()
        token = proxy.register_tool_scope(
            context_name="default", chat_id=1, thread_id=None, user_id=5,
            tool_factory=lambda: create_openshrimp_tools(bot=bot, chat_id=1),
        )
        url = proxy.get_tools_url(token, "127.0.0.1")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert "send_file" in names
    finally:
        await proxy.shutdown()


# --- Registry unit ----------------------------------------------------------


async def test_register_tool_scope_stable_per_key() -> None:
    registry = ProxyRegistry()
    factory = lambda: []  # noqa: E731
    t1 = registry.register_tool_scope(
        context_name="c", chat_id=1, thread_id=None, user_id=5,
        tool_factory=factory,
    )
    t2 = registry.register_tool_scope(
        context_name="c", chat_id=1, thread_id=None, user_id=5,
        tool_factory=factory,
    )
    assert t1 == t2
    assert registry.get_tool_scope(t1) is not None

    registry.unregister_context("c")
    assert registry.get_tool_scope(t1) is None
