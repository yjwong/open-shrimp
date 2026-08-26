"""The OpenCode models the setup wizards offer, and the labs behind them.

Not :meth:`OpenCodeBackend.model_catalog`, whose emptiness is load-bearing:
``/model`` draws a picker only for a non-empty catalog and ``config_app`` serves
the live provider catalog instead, so putting four entries there would replace
a 300-model list with a shortlist at both call sites.

A shortlist rather than that live catalog because setup runs before there is a
config and usually before the pinned CLI is on the machine: a menu that needs a
50 MB download and an HTTP round trip before it can be drawn is a menu that
fails on hotel wifi.  It drifts, and both ``/model <id>`` and the config Mini
App accept anything, so the drift costs a menu row rather than a capability.

Ids resolved against ``models.dev/api.json``, the catalog opencode itself
resolves ids against, so an id here is an id opencode accepts.  Each carries
``tool_call`` and ``reasoning``: a model that cannot call tools cannot serve a
turn.  One model per lab, across a 10× price range, because the reason to leave
Claude is usually either a subscription already paid for or a bill being cut.

``gpt-5.6-sol`` rather than the bare ``gpt-5.6``: models.dev carries both, and
they differ in nothing but the id and the display name, so the bare id is today
a pointer at the Sol tier alongside Terra and Luna.  The config records the tier
for the same reason ``MODEL_CHOICES`` records ``claude-opus-5``: what a pointer
resolves to is somebody else's to change.
"""

from __future__ import annotations

from open_shrimp.backend.protocol import ModelChoice


def _choice(model: str, description: str) -> ModelChoice:
    """One shortlist row.  The alias and the wire id are the same string: an
    OpenCode model is addressed as ``provider/model`` and has no short form to
    expand."""
    return ModelChoice(model, model, description)


SETUP_MODEL_CHOICES: tuple[ModelChoice, ...] = (
    _choice("openai/gpt-5.6-sol", "Frontier GPT for coding and agentic work"),
    _choice("google/gemini-3.7-flash", "Fast, 1M context, cheap"),
    _choice("xai/grok-4.6", "Long-running agents and coding"),
    _choice("deepseek/deepseek-v4-pro", "A tenth the price, 1M context"),
)

# Provider id → the lab as a menu names it.  Written out rather than derived
# from the ids above, because "xai" is not "xAI" and "deepseek" is not
# "DeepSeek": capitalisation is the lab's to choose, and every one of these is
# a name somebody will see beside the account they are about to log into.
_PROVIDER_NAMES: dict[str, str] = {
    "openai": "OpenAI",
    "google": "Google",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "azure": "Azure",
    "amazon-bedrock": "Amazon Bedrock",
}


def provider_label(provider_id: str) -> str:
    """The lab as a menu names it.

    Its own id where there is no name for it, which is what opencode prints and
    what its auth file is keyed by — so a provider outside the table above is
    named something a user can still match against what the CLI shows them.
    """
    return _PROVIDER_NAMES.get(provider_id, provider_id)


__all__ = ["SETUP_MODEL_CHOICES", "provider_label"]
