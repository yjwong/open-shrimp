from __future__ import annotations

import pytest

from open_shrimp.db import ChatScope
from open_shrimp.handlers import messages
from open_shrimp.handlers.state import _pending_injected_responses


class _FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)


class _FakeSession:
    def __init__(self) -> None:
        self.client = _FakeClient()
        self.sandbox = None


def test_pending_injected_responses_do_not_continue_without_followup() -> None:
    scope = ChatScope(chat_id=1, thread_id=None)
    _pending_injected_responses.clear()

    assert messages._consume_pending_injected_response(scope) is False
    assert scope not in _pending_injected_responses


def test_pending_injected_responses_consume_one_at_a_time() -> None:
    scope = ChatScope(chat_id=1, thread_id=2)
    _pending_injected_responses.clear()

    messages._add_pending_injected_responses(scope)
    messages._add_pending_injected_responses(scope, 2)

    assert _pending_injected_responses[scope] == 3
    assert messages._consume_pending_injected_response(scope) is True
    assert _pending_injected_responses[scope] == 2
    assert messages._consume_pending_injected_response(scope) is True
    assert _pending_injected_responses[scope] == 1
    assert messages._consume_pending_injected_response(scope) is True
    assert scope not in _pending_injected_responses
    assert messages._consume_pending_injected_response(scope) is False


@pytest.mark.asyncio
async def test_live_message_injection_does_not_force_extra_receive() -> None:
    scope = ChatScope(chat_id=10, thread_id=20)
    session = _FakeSession()
    _pending_injected_responses.clear()

    await messages._inject_message(session, "follow up", [], scope, bot=None)

    assert session.client.prompts == ["follow up"]
    assert scope not in _pending_injected_responses
