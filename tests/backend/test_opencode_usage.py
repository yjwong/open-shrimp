from __future__ import annotations

import pytest

from open_shrimp.backend import types as bt
from open_shrimp.backend.opencode.client import _context_window_from_models
from open_shrimp.backend.opencode.sse import EventQueueClosed
from open_shrimp.backend.opencode.translate import _iter_response
from open_shrimp.config import Config, ContextConfig, TelegramConfig
from open_shrimp.handlers.status_render import build_pinned_status


class _FakeQueue:
    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    async def get(self) -> dict:
        if not self._events:
            raise EventQueueClosed
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_usage_is_normalised_for_status_rendering() -> None:
    queue = _FakeQueue([
        {
            "type": "message.updated",
            "properties": {
                "info": {
                    "role": "assistant",
                    "id": "step-1",
                    "finish": "stop",
                    "modelID": "gpt-5.5",
                    "tokens": {
                        "input": 10_000,
                        "output": 500,
                        "reasoning": 100,
                        "cache": {"read": 20_000, "write": 2_000},
                    },
                    "cost": 0.1234,
                }
            },
        },
        {"type": "session.idle", "properties": {}},
    ])

    messages = [
        message
        async for message in _iter_response(
            queue, "session-1", None, None, None, 400_000
        )
    ]
    assistant = next(m for m in messages if isinstance(m, bt.AssistantMessage))
    result = next(m for m in messages if isinstance(m, bt.ResultMessage))

    assert assistant.usage == {
        "input_tokens": 10_000,
        "output_tokens": 500,
        "reasoning_tokens": 100,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": 20_000,
    }
    assert result.usage == assistant.usage
    assert result.model_usage == {
        "gpt-5.5": {
            "inputTokens": 10_000,
            "outputTokens": 500,
            "reasoningTokens": 100,
            "cacheCreationInputTokens": 2_000,
            "cacheReadInputTokens": 20_000,
            "costUSD": 0.1234,
            "contextWindow": 400_000,
        }
    }

    ctx = ContextConfig(
        directory="/tmp/project",
        description="Project",
        allowed_tools=[],
        backend="opencode",
        model="openai/gpt-5.5",
    )
    config = Config(
        telegram=TelegramConfig(token="test"),
        allowed_users=[1],
        contexts={"default": ctx},
        default_context="default",
        backend="opencode",
    )
    status = build_pinned_status(
        "default", ctx, config, result.model_usage, assistant.usage
    )
    assert "32.0k / 400.0k" in status
    assert "8%" in status
    assert "$0.1234" in status


def test_context_window_matches_provider_and_model_id() -> None:
    models = [
        {
            "providerID": "other",
            "id": "gpt-5.5",
            "limit": {"context": 100_000},
        },
        {
            "providerID": "openai",
            "apiID": "gpt-5.5",
            "limit": {"context": 400_000},
        },
    ]

    assert _context_window_from_models(models, "openai", "gpt-5.5") == 400_000


@pytest.mark.parametrize("context", [None, True, 0, -1, "400000"])
def test_context_window_rejects_invalid_limits(context: object) -> None:
    models = [{
        "providerID": "openai",
        "id": "gpt-5.5",
        "limit": {"context": context},
    }]

    assert _context_window_from_models(models, "openai", "gpt-5.5") is None
