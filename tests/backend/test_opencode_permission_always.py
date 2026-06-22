"""Tests for the ``permission.asked.always`` arm in the OpenCode bridge.

OpenCode's ``always`` array carries *candidate* "always allow" globs — what
OpenCode would persist **if** the user chose "always" (e.g. ``git *`` for a
``git status`` call), not patterns the user has already approved.  The bridge
must therefore never auto-apply them: every ``permission.asked`` event routes
through ``can_use_tool`` so the user is actually prompted.  The candidate
patterns are surfaced to the approval UI via
``ToolPermissionContext.suggestions`` / ``always_patterns`` so the keyboard
can offer "always allow" buttons.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from open_shrimp.backend.opencode.permission import PermissionBridge
from open_shrimp.backend.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)


def _make_bridge(*, can_use_tool: Any = None) -> PermissionBridge:
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
    )


def _make_event(
    *,
    request_id: str = "req-1",
    permission: str = "bash",
    always: list[str] | None = None,
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


def _record_can_use_tool() -> tuple[list[ToolPermissionContext], Any]:
    """A ``can_use_tool`` that records each ctx and approves once."""
    seen: list[ToolPermissionContext] = []

    async def _cb(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> Any:
        seen.append(ctx)
        return PermissionResultAllow(reply="once")

    return seen, _cb


@pytest.mark.asyncio
async def test_always_pattern_does_not_auto_approve() -> None:
    """An ``always: ["git *"]`` arm still routes through ``can_use_tool`` —
    the user is prompted, the pattern is not silently pre-approved."""
    seen, can_use_tool = _record_can_use_tool()

    bridge = _make_bridge(can_use_tool=can_use_tool)
    # Seed the ToolPart cache so the resolver returns the bash tool name
    # without touching the network.
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git status"}},
    })

    evt = _make_event(always=["git *"])
    await bridge._do_handle_permission_asked(evt)

    # can_use_tool was consulted exactly once (no auto-approve shortcut).
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_always_patterns_surfaced_as_suggestions() -> None:
    """Every candidate pattern reaches the approval UI as a suggestion."""
    seen, can_use_tool = _record_can_use_tool()

    bridge = _make_bridge(can_use_tool=can_use_tool)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "git pull"}},
    })

    evt = _make_event(always=["git *", "npm *"])
    await bridge._do_handle_permission_asked(evt)

    assert len(seen) == 1
    assert seen[0].suggestions == ["git *", "npm *"]
    assert seen[0].always_patterns == ["git *", "npm *"]


@pytest.mark.asyncio
async def test_deny_is_respected_despite_always_patterns() -> None:
    """A denial from ``can_use_tool`` stands — candidate ``always`` patterns
    never override the user's decision."""
    async def _deny(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> Any:
        return PermissionResultDeny(message="nope")

    bridge = _make_bridge(can_use_tool=_deny)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "rm -rf /"}},
    })

    evt = _make_event(always=["rm *"])
    # The cached call approval should reflect the deny, not an allow.
    await bridge._do_handle_permission_asked(evt)
    cached = bridge._get_call_approval("call-1", "bash", {"command": "rm -rf /"})
    assert isinstance(cached, PermissionResultDeny)


@pytest.mark.asyncio
async def test_empty_always_still_prompts() -> None:
    """No candidate patterns means empty suggestions, but the user is still
    asked."""
    seen, can_use_tool = _record_can_use_tool()

    bridge = _make_bridge(can_use_tool=can_use_tool)
    bridge.observe_tool_part({
        "type": "tool",
        "callID": "call-1",
        "tool": "bash",
        "messageID": "msg-1",
        "state": {"status": "running", "input": {"command": "ls"}},
    })

    evt = _make_event(always=[])
    await bridge._do_handle_permission_asked(evt)

    assert len(seen) == 1
    assert seen[0].suggestions == []


@pytest.mark.asyncio
async def test_non_list_always_field_is_discarded() -> None:
    """A malformed ``always`` field (e.g. a string) yields empty suggestions
    rather than crashing, and the user is still prompted."""
    seen, can_use_tool = _record_can_use_tool()

    bridge = _make_bridge(can_use_tool=can_use_tool)
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

    assert len(seen) == 1
    assert seen[0].suggestions == []
