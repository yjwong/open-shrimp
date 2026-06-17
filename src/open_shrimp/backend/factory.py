"""Config-driven backend selection.

One global ``backend:`` key, chosen once at startup and resolved like
``SandboxManager``.  :data:`_BACKENDS` is the entire selection surface — a new
backend is one dict entry mapping its name to a zero-arg constructor.

The backend object is a stateless factory — construct it once (at startup,
beside the config load and ``SandboxManager`` build) and thread it down; do
**not** call ``get_backend`` per message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from open_shrimp.backend.claude_sdk import ClaudeSdkBackend
from open_shrimp.backend.protocol import Backend

# The default when the ``backend:`` key is absent — so existing configs with no
# ``backend:`` behave identically (zero behavior change for every deployment).
DEFAULT_BACKEND = "claude_sdk"

_BACKENDS: dict[str, Callable[[], Backend]] = {
    "claude_sdk": lambda: ClaudeSdkBackend(),
}


def known_backends() -> list[str]:
    """The registered backend names (for config validation / error messages)."""
    return sorted(_BACKENDS)


def get_backend(config: Any) -> Backend:
    """Resolve the backend named by ``config['backend']`` (default ``claude_sdk``).

    ``config`` may be a mapping (raw YAML) or any object with a ``backend``
    attribute; absent / falsy selects the default.  Raises ``ValueError`` for
    an unknown name so a typo fails fast at startup.
    """
    if isinstance(config, dict):
        name = config.get("backend") or DEFAULT_BACKEND
    else:
        name = getattr(config, "backend", None) or DEFAULT_BACKEND
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(
            f"Unknown backend {name!r}; known: {known_backends()}"
        ) from None


__all__ = ["DEFAULT_BACKEND", "get_backend", "known_backends"]
