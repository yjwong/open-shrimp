from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_shrimp.client_manager import AgentSession, receive_events, submit_query
from open_shrimp.backend.types import ResultMessage, SystemMessage


pytestmark = pytest.mark.asyncio


async def test_submit_query_marks_turn_active_until_result() -> None:
    async def responses():
        yield SystemMessage(subtype="init", data={})
        yield ResultMessage(session_id="session-1")

    client = SimpleNamespace(
        query=AsyncMock(),
        receive_response=responses,
    )
    session = AgentSession(client=client)

    await submit_query(session, "install Windows")

    assert session.in_turn is True
    events = receive_events(session)
    assert isinstance(await anext(events), SystemMessage)
    assert session.in_turn is True
    assert isinstance(await anext(events), ResultMessage)
    assert session.in_turn is False


async def test_failed_query_restores_existing_turn_state() -> None:
    client = SimpleNamespace(query=AsyncMock(side_effect=RuntimeError("closed")))
    session = AgentSession(client=client, in_turn=True)

    with pytest.raises(RuntimeError, match="closed"):
        await submit_query(session, "injected message")

    assert session.in_turn is True


async def test_failed_initial_query_does_not_leave_turn_active() -> None:
    client = SimpleNamespace(query=AsyncMock(side_effect=RuntimeError("closed")))
    session = AgentSession(client=client)

    with pytest.raises(RuntimeError, match="closed"):
        await submit_query(session, "first message")

    assert session.in_turn is False
