"""Claude model registry — the aliases this backend offers and what they mean.

Resolving aliases here fixes model identity in OpenShrimp's config instead of
deferring to the alias table baked into whichever CLI binary serves the turn.
That binary differs per context: unsandboxed contexts use the CLI bundled in
``claude-agent-sdk``, sandboxed ones the guest's own install.

Aliases are bare — no ``[1m]`` suffix.  Every model here has a 1M context
window natively, and the suffix is entitlement-gated.
"""

from __future__ import annotations

from open_shrimp.backend.protocol import ModelChoice

# Alias, wire ID, and menu blurb in one place, in menu order.
MODEL_CHOICES: tuple[ModelChoice, ...] = (
    ModelChoice(
        "fable",
        "claude-fable-5-1",
        "Most capable for your hardest and longest-running tasks",
    ),
    ModelChoice("opus", "claude-opus-5", "Best for everyday, complex tasks"),
    ModelChoice(
        "sonnet",
        "claude-sonnet-5",
        "Efficient for routine tasks, recommended for most coding",
    ),
    ModelChoice("haiku", "claude-haiku-4-5", "Fastest for quick answers"),
)

_BY_ALIAS: dict[str, str] = {c.alias: c.model_id for c in MODEL_CHOICES}

# IDs accepted without a warning beyond the ones the aliases already name.
_ALSO_KNOWN: frozenset[str] = frozenset(
    {
        "claude-fable-5",
        "claude-mythos-5-1",
        "claude-mythos-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
    }
)

KNOWN_MODEL_IDS: frozenset[str] = frozenset(_BY_ALIAS.values()) | _ALSO_KNOWN


def normalize_model(model: str | None) -> str | None:
    """Map an alias to its model ID; pass anything else through unchanged."""
    if model is None:
        return None
    return _BY_ALIAS.get(model.strip().lower(), model)


def is_known_model(model: str) -> bool:
    """Whether *model* is a known alias or a recognised model ID."""
    resolved = normalize_model(model)
    return resolved is not None and resolved.strip().lower() in KNOWN_MODEL_IDS
