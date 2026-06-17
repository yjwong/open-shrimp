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
