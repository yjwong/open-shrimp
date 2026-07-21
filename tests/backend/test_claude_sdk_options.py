"""Options-translation tests for the claude_sdk adapter."""

from __future__ import annotations

import claude_agent_sdk.types as sdk_types
import pytest

from open_shrimp.backend import types as bt
from open_shrimp.backend.claude_sdk.options import translate_options
from open_shrimp.backend.protocol import BackendOptions


def test_honoured_fields_map_1_to_1():
    opts = BackendOptions(
        cwd="/work",
        model="claude-x",
        effort="high",
        allowed_tools=["Bash(git *)"],
        disallowed_tools=["WebSearch"],
        add_dirs=["/extra"],
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        max_buffer_size=1234,
        cli_path="/usr/bin/wrapper",
    )
    sdk = translate_options(opts)
    assert sdk.cwd == "/work"
    assert sdk.model == "claude-x"
    assert sdk.effort == "high"
    assert sdk.allowed_tools == ["Bash(git *)"]
    assert sdk.disallowed_tools == ["WebSearch"]
    assert sdk.add_dirs == ["/extra"]
    assert sdk.setting_sources == ["project", "user", "local"]
    assert sdk.include_partial_messages is True
    assert sdk.max_buffer_size == 1234
    assert sdk.cli_path == "/usr/bin/wrapper"


def test_preset_dict_system_prompt_passes_through():
    """The live SDK path passes a preset-dict, not a plain string."""
    preset = {"type": "preset", "preset": "claude_code", "append": "extra"}
    opts = BackendOptions(cwd="/w", system_prompt=preset)
    sdk = translate_options(opts)
    assert sdk.system_prompt == preset


def test_str_system_prompt_passes_through():
    opts = BackendOptions(cwd="/w", system_prompt="plain")
    sdk = translate_options(opts)
    assert sdk.system_prompt == "plain"


def test_absent_system_prompt_not_forced():
    """When unset, the SDK options keep their own default (not None-clobbered)."""
    opts = BackendOptions(cwd="/w")
    sdk = translate_options(opts)
    # Default-constructed BackendOptions has system_prompt=None, so the SDK
    # default is left untouched (translate only assigns when present).
    default = type(sdk)(cwd="/w").system_prompt
    assert sdk.system_prompt == default


def test_resume_and_mcp_servers_only_set_when_present():
    handle = {"openshrimp": {"type": "http", "url": "http://x"}}
    opts = BackendOptions(cwd="/w", resume="sess-1", mcp_servers=handle)
    sdk = translate_options(opts)
    assert sdk.resume == "sess-1"
    assert sdk.mcp_servers == handle


def test_extra_is_ignored_by_sdk_backend():
    opts = BackendOptions(cwd="/w", extra={"opencode_endpoint": "http://y"})
    sdk_opts = translate_options(opts)
    # No attribute leak: extra never reaches the SDK constructor.
    assert not hasattr(sdk_opts, "extra") or (
        getattr(sdk_opts, "extra", None) != opts.extra
    )


def test_absent_can_use_tool_stays_none():
    """Scheduled / read-only sessions pass no callback; translation must not
    wrap ``None`` (which would crash the SDK when it tries to call it)."""
    opts = BackendOptions(cwd="/w")
    assert opts.can_use_tool is None
    sdk_opts = translate_options(opts)
    assert sdk_opts.can_use_tool is None


@pytest.mark.asyncio
async def test_can_use_tool_is_wrapped_for_sdk_contract():
    """A neutral-returning callback is wrapped so the SDK sees SDK-typed
    results — the boundary that keeps the live SDK path from raising
    ``TypeError`` on the neutral return value."""

    async def neutral(tool_name, tool_input, context):
        return bt.PermissionResultAllow()

    opts = BackendOptions(cwd="/w", can_use_tool=neutral)
    sdk_opts = translate_options(opts)

    # The wired callback is a different object (wrapped), and its output
    # satisfies the SDK's isinstance contract.
    assert sdk_opts.can_use_tool is not neutral
    result = await sdk_opts.can_use_tool("Bash", {"command": "ls"}, object())
    assert isinstance(result, sdk_types.PermissionResultAllow)
