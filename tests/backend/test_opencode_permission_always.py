"""Tests for the ``permission.asked.always`` arm in the OpenCode bridge.

When OpenCode emits a ``permission.asked`` event with an ``always`` array
populated, those patterns represent the user's durable "always allow"
choice (made through OpenCode's own UI, or replayed by OpenCode for an
already-durable rule).  The bridge pre-registers each pattern as a
session-scoped rule via the ``register_session_rule`` callback so the
choice takes effect immediately inside the current turn — sibling tool
calls behind the same prefix auto-resolve without prompting the user.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from open_shrimp.backend.opencode.permission import PermissionBridge
from open_shrimp.backend.types import (
    PermissionResultAllow,
    ToolPermissionContext,
)


def _make_bridge(
    *,
    register_session_rule: Any = None,
    can_use_tool: Any = None,
) -> PermissionBridge:
    http = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={}),
    ))
    if can_use_tool is None:
        async def _allow(
            tool_name: str,
            tool_input: dict[str, Any],
            ctx: ToolPermissionContext,
        ) -> Any:
            return PermissionResultAllow(reply="once")

        can_use_tool = _allow
    return PermissionBridge(
        http=http,
        can_use_tool=can_use_tool,
        session_id="sess-1",
        directory="/work",
        register_session_rule=register_session_rule,
    )


def _make_event(
    *,
    request_id: str = "req-1",
    permission: str = "bash",
    always: list[str] | None = None,
    tool_name: str = "bash",
    tool_input: dict[str, Any] | None = None,
    call_id: str = "call-1",
) -> dict[str, Any]:
    return {
        "type": "permission.asked",
        "properties": {
            "id": request_id,
            "sessionID": "sess-1",
            "permission": permission,
            "always": list(always) if always else [],
            "metadata": tool_input or {},
            "tool": {"callID": call_id, "messageID": "msg-1"},
        },
    }


@pytest.mark.asyncio
async def test_always_pattern_pre_registers_via_callback() -> None:
    """An ``always: ["git *"]`` arm fires ``register_session_rule`` once
    per pattern with the resolved native tool name."""
    seen: list[tuple[str, str]] = []

    def _register(tool_name: str, pattern: str) -> None:
        seen.append((tool_name, pattern))

    bridge = _make_bridge(register_session_rule=_register)
    # Seed the ToolPart cache so the resolver returns the bash tool name
    # without touching the network.
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git status"}},
    })

    evt = _make_event(always=["git *"], tool_name="bash")
    await bridge._do_handle_permission_asked(evt)

    assert seen == [("bash", "git *")]


@pytest.mark.asyncio
async def test_multiple_always_patterns_each_fire_callback() -> None:
    """Every pattern in the ``always`` array hits the callback in order."""
    seen: list[tuple[str, str]] = []

    def _register(tool_name: str, pattern: str) -> None:
        seen.append((tool_name, pattern))

    bridge = _make_bridge(register_session_rule=_register)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git pull"}},
    })

    evt = _make_event(always=["git *", "npm *"])
    await bridge._do_handle_permission_asked(evt)

    assert seen == [("bash", "git *"), ("bash", "npm *")]


@pytest.mark.asyncio
async def test_empty_always_does_not_call_register() -> None:
    """No ``always`` patterns means the callback is never invoked."""
    register = AsyncMock()

    bridge = _make_bridge(register_session_rule=register)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "ls"}},
    })

    evt = _make_event(always=[])
    await bridge._do_handle_permission_asked(evt)

    register.assert_not_called()


@pytest.mark.asyncio
async def test_no_callback_set_silently_skips() -> None:
    """``register_session_rule=None`` means the arm is dormant — no crash,
    no log noise, the regular approval flow still runs."""
    bridge = _make_bridge(register_session_rule=None)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git status"}},
    })

    evt = _make_event(always=["git *"])
    # Just shouldn't raise.
    await bridge._do_handle_permission_asked(evt)


@pytest.mark.asyncio
async def test_register_callback_exception_does_not_break_approval() -> None:
    """A raising ``register_session_rule`` is caught and logged so the
    approval flow still completes."""

    def _register(tool_name: str, pattern: str) -> None:
        raise RuntimeError("boom")

    can_use_tool = AsyncMock(return_value=PermissionResultAllow(reply="once"))
    bridge = _make_bridge(
        register_session_rule=_register, can_use_tool=can_use_tool,
    )
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git status"}},
    })

    evt = _make_event(always=["git *"])
    await bridge._do_handle_permission_asked(evt)

    # can_use_tool was still invoked despite the register callback crash.
    can_use_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_list_always_field_does_not_call_register() -> None:
    """A malformed ``always`` field (e.g. a string instead of a list) is
    discarded — no spurious callback firing."""
    register = AsyncMock()
    bridge = _make_bridge(register_session_rule=register)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "ls"}},
    })

    evt = _make_event()
    evt["properties"]["always"] = "git *"  # wrong shape
    await bridge._do_handle_permission_asked(evt)

    register.assert_not_called()


@pytest.mark.asyncio
async def test_register_uses_resolved_native_tool_name() -> None:
    """The callback receives the native (lowercase) OpenCode tool name,
    not the permission category — confirming the resolution order."""
    seen: list[tuple[str, str]] = []

    def _register(tool_name: str, pattern: str) -> None:
        seen.append((tool_name, pattern))

    bridge = _make_bridge(register_session_rule=_register)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "edit",  # native OpenCode name
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"filePath": "/work/x.py"}},
    })

    evt = _make_event(
        permission="edit", always=["/work/**"], call_id="call-1",
    )
    await bridge._do_handle_permission_asked(evt)

    assert seen == [("edit", "/work/**")]
