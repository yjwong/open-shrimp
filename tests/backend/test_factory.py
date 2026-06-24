"""Factory + protocol-conformance tests for the backend seam."""

from __future__ import annotations

import pytest

from open_shrimp.backend import (
    Backend,
    BackendClient,
    BackendOptions,
    get_backend,
    known_backends,
)
from open_shrimp.backend.claude_sdk import ClaudeSdkBackend


def test_default_backend_is_claude_sdk():
    """Absent ``backend:`` key resolves to claude_sdk (zero-config behavior)."""
    b = get_backend({})
    assert b.name == "claude_sdk"
    assert isinstance(b, ClaudeSdkBackend)


def test_explicit_claude_sdk_resolves():
    assert get_backend({"backend": "claude_sdk"}).name == "claude_sdk"


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown backend 'nope'"):
        get_backend({"backend": "nope"})


def test_get_backend_accepts_attr_object():
    """``get_backend`` accepts a Config-like object with a ``backend`` attr."""

    class _Cfg:
        backend = "claude_sdk"

    assert get_backend(_Cfg()).name == "claude_sdk"


def test_known_backends_lists_claude_sdk():
    assert "claude_sdk" in known_backends()


def test_backend_satisfies_protocol():
    """``ClaudeSdkBackend`` is a runtime-checkable ``Backend``."""
    assert isinstance(ClaudeSdkBackend(), Backend)


def test_make_client_returns_backend_client():
    """``make_client`` returns something satisfying ``BackendClient`` and does
    NOT connect (no subprocess spawned at construction)."""
    b = ClaudeSdkBackend()
    client = b.make_client(BackendOptions(cwd="/tmp"))
    assert isinstance(client, BackendClient)


def test_make_tool_server_selects_shared_bridge_installer():
    """For claude_sdk the selector returns the shared HTTP-bridge installer."""
    from open_shrimp.backend.tools import serve_tools_over_mcp_http

    b = ClaudeSdkBackend()
    installer = b.make_tool_server(lambda: [])
    assert installer is serve_tools_over_mcp_http


class _FakeProxy:
    def register_tool_scope(self, **kwargs):
        return "scope-token"

    def get_tools_url(self, token, host_ip):
        return f"http://{host_ip}/tools/{token}"


_SCOPE = dict(
    context_name="c", chat_id=1, thread_id=None, user_id=2, host_ip="127.0.0.1",
)


def test_claude_tool_server_omits_request_timeout():
    """claude_sdk streams slow calls over SSE, so it pins no MCP timeout."""
    install = ClaudeSdkBackend().make_tool_server(lambda: [])
    config = install(_FakeProxy(), lambda: [], **_SCOPE)
    assert config["type"] == "http"
    assert "timeout" not in config


def test_opencode_tool_server_pins_long_request_timeout():
    """opencode pins a long per-request timeout so slow tools don't -32001."""
    from open_shrimp.backend.opencode.backend import OpenCodeBackend

    install = OpenCodeBackend().make_tool_server(lambda: [])
    config = install(_FakeProxy(), lambda: [], **_SCOPE)
    assert config["type"] == "http"
    # Comfortably past ask_context's 600s self-bound.
    assert isinstance(config.get("timeout"), int)
    assert config["timeout"] >= 600_000


# ── get_backend_by_name: memoisation across distinct names ──


def test_get_backend_by_name_memoises():
    """A second call for the same name returns the cached instance."""
    from open_shrimp.backend import get_backend_by_name

    a = get_backend_by_name("claude_sdk")
    b = get_backend_by_name("claude_sdk")
    assert a is b


def test_get_backend_by_name_unknown_raises():
    from open_shrimp.backend import get_backend_by_name

    with pytest.raises(ValueError, match="Unknown backend 'nope'"):
        get_backend_by_name("nope")


def test_get_backend_uses_same_cache_as_by_name():
    """The convenience wrapper hits the same memo as ``get_backend_by_name``."""
    from open_shrimp.backend import get_backend, get_backend_by_name

    direct = get_backend_by_name("claude_sdk")
    via_config = get_backend({"backend": "claude_sdk"})
    assert direct is via_config
