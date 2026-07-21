"""``BackendOptions`` → OpenCode-native options translation (opencode adapter).

A pure function that maps the backend-neutral ``BackendOptions`` into the
native option shape this backend consumes (``OpenCodeOptions``).  This is the
only place that knows OpenCode's option vocabulary; ``client.py`` reads the
native shape.

Honoured-intersection mapping:

* ``cwd``, ``resume``, ``effort``, ``allowed_tools``, ``disallowed_tools``,
  ``add_dirs``, ``can_use_tool``, ``stderr``, ``system_prompt`` → direct.
* ``model`` → ``split_provider_model(model)`` → ``(provider, model)``.  An
  unqualified ``model`` **raises** here (fail fast at connect — OpenCode
  requires ``provider/model``).
* SDK-only fields (``setting_sources``, ``include_partial_messages``,
  ``max_buffer_size``, ``cli_path``) → accept-but-ignore.
* ``mcp_servers`` → carried through; the client registers these server-side
  (the shared HTTP-bridge handle from ``make_tool_server`` is one of them).

Two seams: ``endpoint`` and ``handle_questions`` are not ``BackendOptions``
fields, so they ride in ``opts.extra``:

* ``extra["endpoint"]`` → the sandbox-provided ``OpenCodeEndpoint | None``.
  Unset on non-sandboxed contexts → the client spawns the host-local
  ``OpenCodeServer``.
* ``extra["handle_questions"]`` → the native ``question.asked`` callback, or
  ``None``.

``split_provider_model`` is kept here — the only surviving piece of the old
``opencode_client/options.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from open_shrimp.backend.opencode.process import OpenCodeEndpoint
from open_shrimp.backend.protocol import BackendOptions


def split_provider_model(model: str | None) -> tuple[str, str]:
    """Split a context's ``model`` field into ``(provider, model)``.

    Expects ``provider/model``. Raises if the field is missing or
    unqualified — open-shrimp configs must name a provider explicitly
    under OpenCode.
    """
    if not model:
        raise ValueError(
            "context.model is required and must be 'provider/model'"
        )
    if "/" not in model:
        raise ValueError(
            f"context.model {model!r} must be 'provider/model' "
            f"(e.g. 'openai/gpt-5.5', 'google/gemini-2.5-pro')"
        )
    provider, _, rest = model.partition("/")
    return provider, rest


@dataclass
class OpenCodeOptions:
    """The OpenCode-native option shape ``OpenCodeClient`` consumes.

    Produced by :func:`to_opencode`; never built by call sites directly.
    """

    cwd: str
    provider: str
    model: str
    resume: str | None = None
    endpoint: OpenCodeEndpoint | None = None

    # Honoured fields.
    effort: str | None = None  # → variant on prompt_async
    allowed_tools: list[str] | None = None  # → session permission rules
    disallowed_tools: list[str] | None = None  # → last-position deny rules
    add_dirs: list[str] | None = None  # → external_directory allow rules
    stderr: Callable[[str], None] | None = None
    can_use_tool: Callable[..., Any] | None = None
    handle_questions: (
        Callable[[list[dict[str, Any]]], Awaitable[list[list[str]]]] | None
    ) = None
    system_prompt: str | None = None  # → system on prompt_async

    # Accepted-but-ignored fields for older OpenShrimp call sites.
    setting_sources: list[str] | None = None
    include_partial_messages: bool = True
    max_buffer_size: int | None = None
    cli_path: str | None = None
    mcp_servers: dict[str, Any] | None = None

    extra: dict[str, Any] = field(default_factory=dict)


def to_opencode(opts: BackendOptions) -> OpenCodeOptions:
    """Map a backend-neutral ``BackendOptions`` to ``OpenCodeOptions``.

    ``model`` must be provider-qualified (``provider/model``); an unqualified
    or missing value raises here (fail fast at connect).  ``endpoint`` and
    ``handle_questions`` are read from ``extra`` (they are not
    honoured-intersection fields).  SDK-only fields are carried through but
    never read.
    """
    provider, model = split_provider_model(opts.model)
    extra = opts.extra or {}
    return OpenCodeOptions(
        cwd=opts.cwd,
        provider=provider,
        model=model,
        resume=opts.resume,
        endpoint=extra.get("endpoint"),
        effort=opts.effort,
        allowed_tools=opts.allowed_tools,
        disallowed_tools=opts.disallowed_tools,
        add_dirs=opts.add_dirs,
        stderr=opts.stderr,
        can_use_tool=opts.can_use_tool,
        handle_questions=extra.get("handle_questions"),
        system_prompt=opts.system_prompt,
        setting_sources=opts.setting_sources,
        include_partial_messages=opts.include_partial_messages,
        max_buffer_size=opts.max_buffer_size,
        cli_path=opts.cli_path,
        mcp_servers=opts.mcp_servers,
        extra=dict(extra),
    )


__all__ = ["OpenCodeOptions", "split_provider_model", "to_opencode"]
