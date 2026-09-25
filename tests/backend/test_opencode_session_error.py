"""``session.error`` from ``opencode serve`` is a provider failure, not a crash.

The handler retries a ``ProcessError`` in a sandboxed context as a dead
sandbox, so the translator must raise ``AgentTurnError`` instead and carry
the provider's message through.
"""

from __future__ import annotations

import pytest

from open_shrimp.backend.errors import AgentTurnError, ProcessError
from open_shrimp.backend.opencode.sse import EventQueueClosed
from open_shrimp.backend.opencode.translate import _iter_response


class _FakeQueue:
    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    async def get(self) -> dict:
        if not self._events:
            raise EventQueueClosed
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_session_error_raises_agent_turn_error_with_provider_message():
    queue = _FakeQueue([
        {
            "type": "session.error",
            "properties": {
                "sessionID": "sess-1",
                "error": {
                    "name": "APIError",
                    "data": {"message": "The usage limit has been reached"},
                },
            },
        },
    ])

    with pytest.raises(AgentTurnError, match="The usage limit has been reached") as info:
        async for _ in _iter_response(queue, "sess-1", None, None, None):
            pass

    assert not isinstance(info.value, ProcessError)
