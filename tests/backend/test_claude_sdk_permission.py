"""Adapter tests for ``to_sdk_permission_callback``.

The shared ``hooks`` path returns ``backend.types`` permission results.  The
SDK consumes the callback's return value with a hard ``isinstance`` check
against *its own* classes (``query.py``) and raises ``TypeError`` otherwise.
``to_sdk_permission_callback`` is the translation boundary that bridges the two.

These tests pin:
- neutral allow → SDK allow, preserving ``updated_input`` / ``updated_permissions``
- neutral deny → SDK deny, preserving ``message`` / ``interrupt``
- the adapter output satisfies the exact ``isinstance`` contract the SDK enforces
- the input ``context`` is passed through unchanged (duck-typed)
"""

from __future__ import annotations

import claude_agent_sdk.types as sdk
import pytest

from open_shrimp.backend import types as bt
from open_shrimp.backend.claude_sdk.permission import to_sdk_permission_callback


@pytest.mark.asyncio
async def test_allow_maps_to_sdk_allow_preserving_fields():
    perms = [object()]

    async def neutral(tool_name, tool_input, context):
        return bt.PermissionResultAllow(
            updated_input={"command": "ls -la"},
            updated_permissions=perms,
        )

    adapted = to_sdk_permission_callback(neutral)
    result = await adapted("Bash", {"command": "ls"}, object())

    assert isinstance(result, sdk.PermissionResultAllow)
    assert result.updated_input == {"command": "ls -la"}
    assert result.updated_permissions is perms


@pytest.mark.asyncio
async def test_bare_allow_maps_to_sdk_allow_with_none_fields():
    """The common ``return PermissionResultAllow()`` site → SDK allow."""

    async def neutral(tool_name, tool_input, context):
        return bt.PermissionResultAllow()

    adapted = to_sdk_permission_callback(neutral)
    result = await adapted("Read", {"file_path": "/x"}, object())

    assert isinstance(result, sdk.PermissionResultAllow)
    assert result.updated_input is None
    assert result.updated_permissions is None


@pytest.mark.asyncio
async def test_deny_maps_to_sdk_deny_preserving_fields():
    async def neutral(tool_name, tool_input, context):
        return bt.PermissionResultDeny(message="nope", interrupt=True)

    adapted = to_sdk_permission_callback(neutral)
    result = await adapted("Write", {"file_path": "/x"}, object())

    assert isinstance(result, sdk.PermissionResultDeny)
    assert result.message == "nope"
    assert result.interrupt is True


@pytest.mark.asyncio
async def test_adapter_output_satisfies_sdk_isinstance_contract():
    """The exact contract ``query.py`` enforces: the return value must be an
    instance of the SDK's own ``PermissionResult`` subclasses — a neutral
    instance would satisfy *neither* branch and crash the turn."""

    async def allow(tool_name, tool_input, context):
        return bt.PermissionResultAllow()

    async def deny(tool_name, tool_input, context):
        return bt.PermissionResultDeny(message="x")

    allow_out = await to_sdk_permission_callback(allow)("T", {}, object())
    deny_out = await to_sdk_permission_callback(deny)("T", {}, object())

    # Mirror query.py's branch selection.
    assert isinstance(allow_out, sdk.PermissionResultAllow)
    assert not isinstance(allow_out, sdk.PermissionResultDeny)
    assert isinstance(deny_out, sdk.PermissionResultDeny)
    assert not isinstance(deny_out, sdk.PermissionResultAllow)

    # And a neutral result would NOT satisfy the SDK check — the very reason
    # the adapter exists.
    assert not isinstance(bt.PermissionResultAllow(), sdk.PermissionResultAllow)


@pytest.mark.asyncio
async def test_context_passed_through_unchanged():
    """The SDK-constructed context is duck-typed by hooks, so the adapter must
    forward it verbatim (no input-side translation)."""
    sentinel = object()
    seen = {}

    async def neutral(tool_name, tool_input, context):
        seen["context"] = context
        return bt.PermissionResultAllow()

    await to_sdk_permission_callback(neutral)("T", {}, sentinel)
    assert seen["context"] is sentinel


@pytest.mark.asyncio
async def test_unexpected_result_raises_typeerror():
    async def neutral(tool_name, tool_input, context):
        return object()

    adapted = to_sdk_permission_callback(neutral)
    with pytest.raises(TypeError, match="unexpected permission result"):
        await adapted("T", {}, object())
