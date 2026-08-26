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


def default_model_label(name: str | None = None) -> str:
    """How the UI names a context that pins no model: "Claude Code default".

    An unset ``model:`` passes no model to the agent at all, so the agent's own
    configuration decides.  The label names that agent: a bare "default" reads
    as a choice OpenShrimp made, and "CLI" does not say which one where two
    backends ship a CLI each.

    *name* is a backend name; absent selects :data:`DEFAULT_BACKEND`, the
    backend a context with no ``backend:`` key is served by.
    """
    return f"{get_backend_by_name(name or DEFAULT_BACKEND).display_name} default"


def backend_for_model(model: str | None) -> str:
    """Which backend serves a context pinned to *model*.

    Asked of the registry rather than pattern-matched, so a third backend routes
    its own models by shipping an ``is_known_model`` and nothing here changes.
    A model no backend claims — including pinning nothing — is the default's,
    which is also what makes an unpinned context Claude's: OpenCode addresses
    models as ``provider/model`` and ``split_provider_model`` raises on a bare
    name, so it can never be the one to serve a turn with no model at all.

    The routing rule for a *first* config, and the reason it lives here rather
    than with the wizards that call it: ``config.build_context_dict`` writes the
    ``backend:`` key from this, and every front end that can create a config
    shares that function.
    """
    if not model:
        return DEFAULT_BACKEND
    for name in known_backends():
        if name != DEFAULT_BACKEND and get_backend_by_name(name).is_known_model(model):
            return name
    return DEFAULT_BACKEND


def provider_id_for_model(model: str | None) -> str | None:
    """The provider a context pinned to *model* needs a credential for.

    ``None`` wherever the credential is not a provider login — an unpinned
    context, or a Claude alias, both of which are signed in as Claude Code.
    """
    if backend_for_model(model) == DEFAULT_BACKEND:
        return None
    from open_shrimp.backend.opencode.options import split_provider_model

    return split_provider_model(model)[0]


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
    "default_model_label",
    "get_backend",
    "get_backend_by_name",
    "known_backends",
]
