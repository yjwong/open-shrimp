from __future__ import annotations

import pytest

from open_shrimp.db import ChatScope
from open_shrimp.opencode_client import CLIConnectionError
from open_shrimp.session_runner import RunnerInput, RunnerState, SessionRunner


class _FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.session_id = "s1"
        self.interrupted = False

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True


class _FakeSession:
    def __init__(self) -> None:
        self.client = _FakeClient()
        self.session_id = "s1"
        self.sandbox = None
        self.last_activity = 1.0


class _DeadClient(_FakeClient):
    async def query(self, prompt: str) -> None:
        raise CLIConnectionError("dead")


class _DeadSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.client = _DeadClient()


def _runner() -> SessionRunner:
    runner = object.__new__(SessionRunner)
    runner.scope = ChatScope(chat_id=1, thread_id=2)
    runner.state = RunnerState(scope=runner.scope, context_name="test")
    runner._startup_buffer = []
    runner._submit_lock = __import__("asyncio").Lock()
    runner._attachment_paths = []
    runner._work_available = __import__("asyncio").Event()
    runner._suppress_prompt_suggestion = False
    runner._task = None
    runner._ready = __import__("asyncio").Event()
    runner._stop = __import__("asyncio").Event()
    return runner


@pytest.mark.asyncio
async def test_submit_buffers_until_session_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    started = False

    async def start() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(runner, "start", start)

    await runner.submit(RunnerInput("hello"))

    assert started is True
    assert [item.prompt for item in runner._startup_buffer] == ["hello"]
    assert runner.state.startup_buffer_depth == 1


@pytest.mark.asyncio
async def test_submit_live_session_queries_immediately() -> None:
    runner = _runner()
    session = _FakeSession()
    runner.state.session = session
    runner._ready = __import__("asyncio").Event()
    runner._ready.set()

    await runner.submit(RunnerInput("steer"))

    assert session.client.prompts == ["steer"]
    assert runner._work_available.is_set()
    assert session.last_activity > 1.0


@pytest.mark.asyncio
async def test_running_submits_track_pending_responses() -> None:
    runner = _runner()
    session = _FakeSession()
    runner.state.session = session
    runner.state.status = "running"
    runner._ready = __import__("asyncio").Event()
    runner._ready.set()

    await runner.submit(RunnerInput("one"))
    await runner.submit(RunnerInput("two"))

    assert session.client.prompts == ["one", "two"]
    assert runner.state.steering_submissions == 2
    assert runner.state.pending_responses == 2
    assert runner._suppress_prompt_suggestion is True
    assert runner._consume_pending_response() is True
    assert runner.state.pending_responses == 1
    assert runner._work_available.is_set()
    assert runner._consume_pending_response() is True
    assert runner.state.pending_responses == 0
    assert runner._work_available.is_set()
    assert runner._consume_pending_response() is False
    assert not runner._work_available.is_set()


@pytest.mark.asyncio
async def test_startup_buffer_extra_telegram_prompts_expect_followups() -> None:
    runner = _runner()
    session = _FakeSession()
    runner.state.session = session
    runner._startup_buffer = [RunnerInput("one"), RunnerInput("two")]
    runner.state.startup_buffer_depth = 2

    await runner._flush_startup_buffer()

    assert session.client.prompts == ["one", "two"]
    assert runner.state.startup_buffer_depth == 0
    assert runner.state.steering_submissions == 1
    assert runner.state.pending_responses == 1
    assert runner._suppress_prompt_suggestion is True
    assert runner._work_available.is_set()


@pytest.mark.asyncio
async def test_cancel_interrupts_current_session() -> None:
    runner = _runner()
    session = _FakeSession()
    runner.state.session = session
    runner._startup_buffer = [RunnerInput("queued")]

    await runner.cancel_current()

    assert session.client.interrupted is True
    assert runner._startup_buffer == []
    assert runner.state.startup_buffer_depth == 0


@pytest.mark.asyncio
async def test_parent_notifications_submit_through_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    session = _FakeSession()
    runner.state.session = session
    runner._ready = __import__("asyncio").Event()
    runner._ready.set()

    async def submit_parent_notifications(session_id: str, submit) -> int:
        assert session_id == "s1"
        await submit("done")
        return 1

    from open_shrimp import agent_tasks

    monkeypatch.setattr(agent_tasks, "submit_parent_notifications", submit_parent_notifications)

    await runner._submit_parent_notifications(session)

    assert session.client.prompts == ["done"]
    assert runner._work_available.is_set()


@pytest.mark.asyncio
async def test_telegram_submit_restarts_after_dead_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    runner.state.session = _DeadSession()
    runner._ready.set()
    closed: list[ChatScope] = []
    started = False

    async def close_session(scope: ChatScope) -> None:
        closed.append(scope)

    async def start() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr("open_shrimp.session_runner.close_session", close_session)
    monkeypatch.setattr(runner, "start", start)

    await runner.submit(RunnerInput("redeliver"))

    assert closed == [runner.scope]
    assert runner.state.session is None
    assert runner.state.status == "starting"
    assert not runner._ready.is_set()
    assert [item.prompt for item in runner._startup_buffer] == ["redeliver"]
    assert started is True


@pytest.mark.asyncio
async def test_agent_notification_dead_transport_propagates() -> None:
    runner = _runner()
    runner.state.session = _DeadSession()
    runner._ready.set()

    with pytest.raises(CLIConnectionError):
        await runner.submit(RunnerInput("notify", source="agent_notification"))

    assert runner._startup_buffer == []
