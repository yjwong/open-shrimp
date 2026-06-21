"""Config-driven backend selection.

A top-level ``backend:`` key chooses the default, and each context may
override it via ``contexts.<name>.backend:``.  :data:`_BACKENDS` is the
entire selection surface — a new backend is one dict entry mapping its
name to a zero-arg constructor.

Backends are stateless factories, but we cache by name so per-backend
providers (MCP config, OAuth) share state across every context that
uses the same backend.  Resolve once at startup (or on first per-context
use) via :func:`get_backend_by_name`; do **not** call the factory per
message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from open_shrimp.backend.claude_sdk import ClaudeSdkBackend
from open_shrimp.backend.opencode import OpenCodeBackend
from open_shrimp.backend.protocol import Backend

# The default when the ``backend:`` key is absent — so existing configs with no
# ``backend:`` behave identically (zero behavior change for every deployment).
DEFAULT_BACKEND = "claude_sdk"

_BACKENDS: dict[str, Callable[[], Backend]] = {
    "claude_sdk": lambda: ClaudeSdkBackend(),
    "opencode": lambda: OpenCodeBackend(),
}

# One ``Backend`` instance per name; the MCP config / OAuth providers cache
# internally, so duplicating instances per context would defeat those caches.
_INSTANCES: dict[str, Backend] = {}


def known_backends() -> list[str]:
    """The registered backend names (for config validation / error messages)."""
    return sorted(_BACKENDS)


def get_backend_by_name(name: str) -> Backend:
    """Return the singleton ``Backend`` instance for *name*, constructing on first use.

    Raises ``ValueError`` for an unknown name so a typo fails fast.
    """
    cached = _INSTANCES.get(name)
    if cached is not None:
        return cached
    try:
        factory = _BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"Unknown backend {name!r}; known: {known_backends()}"
        ) from None
    instance = factory()
    _INSTANCES[name] = instance
    return instance


def get_backend(config: Any) -> Backend:
    """Resolve the backend named by ``config['backend']`` (default ``claude_sdk``).

    ``config`` may be a mapping (raw YAML) or any object with a ``backend``
    attribute; absent / falsy selects the default.  Returns the cached
    instance for that backend name.  Raises ``ValueError`` for an unknown
    name so a typo fails fast at startup.
    """
    if isinstance(config, dict):
        name = config.get("backend") or DEFAULT_BACKEND
    else:
        name = getattr(config, "backend", None) or DEFAULT_BACKEND
    return get_backend_by_name(name)


__all__ = [
    "DEFAULT_BACKEND",
    "get_backend",
    "get_backend_by_name",
    "known_backends",
]
