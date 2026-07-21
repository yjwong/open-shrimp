"""``BackendOptions`` → ``ClaudeAgentOptions`` translation (claude_sdk adapter).

A pure function.  The SDK is where the contract field names came from, so the
honoured fields map 1:1.  ``system_prompt`` passes through as-is (preset-dict
or str).  The SDK-only fields (``setting_sources``, ``include_partial_messages``,
``max_buffer_size``, ``cli_path``) are applied; ``extra`` is ignored by this
backend.

Written as an explicit mapping (not ``ClaudeAgentOptions(**asdict(opts))``) so
the SDK field set stays visible and a future contract field can't silently leak
into the SDK constructor.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from open_shrimp.backend.claude_sdk.permission import to_sdk_permission_callback
from open_shrimp.backend.protocol import BackendOptions


def translate_options(opts: BackendOptions) -> ClaudeAgentOptions:
    """Map a backend-neutral ``BackendOptions`` to the SDK's options object.

    The SDK honours every honoured-intersection field, so this translation is
    total.  ``mcp_servers``, ``resume``, and ``system_prompt`` are only set
    when present.

    ``can_use_tool`` returns ``backend.types`` permission results (the shared
    ``hooks`` path imports no SDK type), so it is wrapped in
    ``to_sdk_permission_callback`` to satisfy the SDK's ``isinstance`` contract
    on the return value.  This is the single chokepoint where the neutral
    callback becomes the SDK's callback.
    """
    can_use_tool = (
        to_sdk_permission_callback(opts.can_use_tool)
        if opts.can_use_tool is not None
        else None
    )
    sdk = ClaudeAgentOptions(
        cwd=opts.cwd,
        model=opts.model,
        effort=opts.effort,
        allowed_tools=opts.allowed_tools,
        disallowed_tools=opts.disallowed_tools,
        add_dirs=opts.add_dirs,
        setting_sources=opts.setting_sources,
        include_partial_messages=opts.include_partial_messages,
        stderr=opts.stderr,
        can_use_tool=can_use_tool,
        cli_path=opts.cli_path,
        max_buffer_size=opts.max_buffer_size,
    )
    if opts.system_prompt is not None:
        sdk.system_prompt = opts.system_prompt
    if opts.mcp_servers is not None:
        sdk.mcp_servers = opts.mcp_servers
    if opts.resume is not None:
        sdk.resume = opts.resume
    return sdk


__all__ = ["translate_options"]
