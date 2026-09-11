import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

from open_shrimp import agent_status_api
from open_shrimp.db import ChatScope
from open_shrimp.handlers import questions as q
from open_shrimp.handlers.state import (
    _pending_other_input, _question_batches, _question_states,
)


@pytest.fixture
def surfaces(monkeypatch):
    send = AsyncMock(return_value=SimpleNamespace(message_id=42))
    close = AsyncMock()
    monkeypatch.setattr(q, "send_rich", send)
    monkeypatch.setattr(q, "_close_card", close)
    monkeypatch.setattr(q, "finalize_and_reset", AsyncMock())
    return send, close


QUESTIONS = [
    {"question": "First?", "options": [{"label": "A"}]},
    {"question": "Second?", "options": [{"label": "B"}]},
    {"question": "Third?", "options": [{"label": "C"}]},
]


@pytest.mark.asyncio
async def test_batch_registered_upfront_and_phone_answers_later_questions(surfaces):
    send, _ = surfaces
    opened = []
    states = []

    async def on_open(qid, text, options, multi, batch_id, index, count):
        opened.append((qid, text, options, multi, batch_id, index, count))
        states.extend(_question_batches[batch_id])
        assert len(states) == 3
        assert len({s.question_id for s in states}) == 3
        assert all(_question_states[s.question_id] is s for s in states)
        for state in reversed(states):
            assert await q.resolve_question_from_device(state.question_id, [0], [])
        assert await q.resolve_question_from_device(qid, [], ["late"]) is None

    closed = AsyncMock()
    result = await q._handle_ask_user_questions(
        None, ChatScope(1), QUESTIONS, None, on_open, closed,
    )
    assert result == {"First?": "A", "Second?": "B", "Third?": "C"}
    assert len(opened) == 1
    assert opened[0][5:] == (0, 3)
    assert send.await_count == 1
    closed.assert_awaited_once()
    assert opened[0][4] not in _question_batches
    assert all(s.question_id not in _question_states for s in states)


@pytest.mark.asyncio
async def test_sequential_cards_keep_stable_batch_and_indexes(surfaces):
    send, _ = surfaces
    opened = []

    async def on_open(qid, text, options, multi, batch_id, index, count):
        opened.append((batch_id, index, count))
        states = _question_batches[batch_id]
        assert [s.future.done() for s in states] == [i < index for i in range(3)]
        _question_states[qid].future.set_result(options[0]["label"])
        assert await q.resolve_question_from_device(qid, [], ["late"]) is None

    await q._handle_ask_user_questions(None, ChatScope(1), QUESTIONS, None, on_open)
    assert send.await_count == 3
    assert len({entry[0] for entry in opened}) == 1
    assert [entry[1:] for entry in opened] == [(0, 3), (1, 3), (2, 3)]


@pytest.mark.asyncio
async def test_answer_during_send_retires_card_and_skips_notification(surfaces):
    send, close = surfaces
    on_open = AsyncMock()

    async def sending(*args, **kwargs):
        qid = kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
        assert await q.resolve_question_from_device(qid, [0], []) == "A"
        close.assert_not_awaited()
        return SimpleNamespace(message_id=42)

    send.side_effect = sending
    assert await q._send_question_keyboard(None, ChatScope(1), QUESTIONS[0], on_open) == "A"
    close.assert_awaited_once()
    assert "Answer:** A" in close.call_args.args[3]
    on_open.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send", "open", "cancel", "draft"])
async def test_batch_cleanup_on_every_exit(surfaces, monkeypatch, failure):
    send, _ = surfaces
    scope = ChatScope(912)
    captured = []
    batch_ids = []

    async def fail(*args, **kwargs):
        batch_id = next(b for b, states in _question_batches.items() if states[0].scope == scope)
        batch_ids.append(batch_id)
        captured.extend(_question_batches[batch_id])
        _pending_other_input[scope] = captured[-1].question_id
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("failure")

    if failure == "send":
        send.side_effect = fail
    elif failure == "draft":
        monkeypatch.setattr(q, "finalize_and_reset", fail)
    closed = AsyncMock()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await q._handle_ask_user_questions(None, scope, QUESTIONS, None, fail, closed)
    assert batch_ids[0] not in _question_batches
    assert scope not in _pending_other_input
    assert all(s.question_id not in _question_states and s.future.cancelled() for s in captured)
    closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_send_failure_cleans_registration(surfaces):
    send, _ = surfaces
    before = set(_question_batches), set(_question_states)
    send.side_effect = RuntimeError("send failed")
    with pytest.raises(RuntimeError):
        await q._send_question_keyboard(None, ChatScope(1), QUESTIONS[0])
    assert (set(_question_batches), set(_question_states)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phone_first", [True, False])
async def test_first_answer_wins_against_telegram_callback(surfaces, monkeypatch, phone_first):
    monkeypatch.setattr(q, "_is_authorized", lambda *_: True)
    batch_id = q._register_question_batch(None, ChatScope(1), QUESTIONS[:1])
    state = _question_batches[batch_id][0]
    query = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock(), message=None)
    try:
        if phone_first:
            assert await q.resolve_question_from_device(state.question_id, [], ["Phone"]) == "Phone"
        await q._handle_question_callback(query, f"q_opt:{state.question_id}:0", None)
        if not phone_first:
            assert await q.resolve_question_from_device(state.question_id, [], ["Phone"]) is None
        assert state.future.result() == ("Phone" if phone_first else "A")
        await q._complete_other_input(None, state, "Too late")
        assert state.future.result() == ("Phone" if phone_first else "A")
    finally:
        q._remove_question_batch(batch_id)


@pytest.mark.asyncio
async def test_cancellation_cleans_batch_without_touching_other_scope(surfaces):
    scope = ChatScope(1)
    other_batch = q._register_question_batch(None, ChatScope(2), QUESTIONS[:1])
    other_state = _question_batches[other_batch][0]
    _pending_other_input[other_state.scope] = other_state.question_id
    opened = asyncio.Event()
    states = []

    async def on_open(qid, text, options, multi, batch_id, index, count):
        states.extend(_question_batches[batch_id])
        _pending_other_input[scope] = qid
        opened.set()

    closed = AsyncMock()
    task = asyncio.create_task(q._handle_ask_user_questions(
        None, scope, QUESTIONS, None, on_open, closed,
    ))
    try:
        await asyncio.wait_for(opened.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(s.future.cancelled() and s.question_id not in _question_states for s in states)
        assert scope not in _pending_other_input
        assert _pending_other_input[other_state.scope] == other_state.question_id
        assert _question_batches[other_batch] == [other_state]
        assert not other_state.future.done()
        closed.assert_awaited_once()
    finally:
        q._remove_question_batch(other_batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_batch_inactive_while_error_card_cleanup_is_blocked(
    surfaces, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _, close = surfaces
    scope = ChatScope(913)
    opened = asyncio.get_running_loop().create_future()
    closing = asyncio.Event()
    release_close = asyncio.Event()

    async def on_open(qid, text, options, multi, batch_id, index, count):
        _pending_other_input[scope] = qid
        opened.set_result((batch_id, list(_question_batches[batch_id])))
        if failure == "error":
            raise RuntimeError("notification failed")

    async def block_close(*args, **kwargs) -> None:
        closing.set()
        await release_close.wait()

    close.side_effect = block_close
    monkeypatch.setattr(agent_status_api, "authenticate_android_request", AsyncMock())
    app = Starlette(routes=agent_status_api.create_agent_status_routes())
    task = asyncio.create_task(q._handle_ask_user_questions(
        None, scope, QUESTIONS, None, on_open,
    ))
    try:
        batch_id, states = await asyncio.wait_for(opened, 1)
        if failure == "cancel":
            task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        assert not task.done()
        assert await q.resolve_question_from_device(states[1].question_id, [0], []) is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/agent/question-batches/{batch_id}")
        assert response.status_code == 404
        assert batch_id not in _question_batches
        assert scope not in _pending_other_input
        assert all(s.future.cancelled() and s.question_id not in _question_states for s in states)
    finally:
        release_close.set()
        result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError if failure == "cancel" else RuntimeError)
