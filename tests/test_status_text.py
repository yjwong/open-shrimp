"""Tests for _build_status_text — OpenCode-native usage shape rendering."""

from __future__ import annotations

import pytest

from open_shrimp.config import ContextConfig
from open_shrimp.handlers.utils import _build_status_text, _resolve_context_window


def _ctx() -> ContextConfig:
    return ContextConfig(
        directory="/tmp/proj",
        description="test context",
        allowed_tools=[],
        model="openai/gpt-5.5",
    )


def test_status_text_renders_native_turn_usage() -> None:
    """OpenCode-native ``turn_usage`` (input + cache) feeds the context bar."""
    turn_usage = {
        "input": 1_000,
        "output": 200,
        "reasoning": 50,
        "cache": {"read": 4_000, "write": 500},
    }
    model_usage = {
        "gpt-5.5": {
            "input": 1_000,
            "output": 200,
            "reasoning": 50,
            "cache": {"read": 4_000, "write": 500},
            "cost": 0.0123,
        },
    }

    text = _build_status_text(
        "alpha", _ctx(), model_usage=model_usage, turn_usage=turn_usage,
    )

    # input(1000) + cache.write(500) + cache.read(4000) = 5500 -> "5.5k"
    # (MarkdownV2-escaped: dots are escaped).
    assert "5\\.5k" in text
    # Cost line uses ``cost``, not ``costUSD``.
    assert "$0\\.0123" in text


def test_status_text_omits_cost_when_zero() -> None:
    model_usage = {
        "m1": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0},
               "cost": 0.0},
    }
    text = _build_status_text(
        "alpha", _ctx(), model_usage=model_usage, turn_usage=None,
    )
    assert "Cost" not in text


def test_status_text_missing_cache_key_does_not_crash() -> None:
    """Defensive: turn_usage without a ``cache`` field still renders."""
    turn_usage = {"input": 100, "output": 10}
    text = _build_status_text(
        "alpha", _ctx(), model_usage=None, turn_usage=turn_usage,
    )
    assert "100" in text


def test_status_text_uses_resolved_context_window() -> None:
    turn_usage = {"input": 10_000, "output": 10, "cache": {"read": 0, "write": 0}}

    text = _build_status_text(
        "alpha", _ctx(), turn_usage=turn_usage, context_window=1_050_000,
    )

    assert r"10\.0k / 1\.1M" in text


@pytest.mark.asyncio
async def test_resolve_context_window_from_opencode_catalog() -> None:
    class FakeClient:
        async def get_models(self) -> list[dict[str, object]]:
            return [
                {
                    "id": "gpt-5.5",
                    "apiID": "gpt-5.5",
                    "providerID": "openai",
                    "limit": {"context": 1_050_000, "input": 922_000, "output": 128_000},
                }
            ]

    assert await _resolve_context_window(_ctx(), FakeClient()) == 1_050_000
