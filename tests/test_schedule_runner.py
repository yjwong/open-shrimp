"""Tests for the events schedule runner.

Covers the per-task-topic run flow: topic self-heal after deletion,
overlap skip notes, timeout cancellation, one-shot auto-delete, and the
migration of an old chat-bound scheduled_tasks table.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiosqlite
import pytest
from telegram.error import BadRequest

from open_shrimp import dispatch_registry
from open_shrimp.db import (
    create_scheduled_task,
    get_all_scheduled_tasks,
    get_event_topic,
    init_db,
    set_event_topic,
)
from open_shrimp.events.schedule import ScheduleRunner, topic_key

EVENTS_CHAT_ID = -100555


class FakeTopic:
    def __init__(self, thread_id: int) -> None:
        self.message_thread_id = thread_id


class FakeBot:
    """Records sent messages; raises topic-gone for threads in gone_threads."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.created_topics: list[str] = []
        self.gone_threads: set[int] = set()
        self._next_thread = 100

    async def create_forum_topic(self, chat_id: int, name: str, **kwargs):
        self.created_topics.append(name)
        self._next_thread += 1
        return FakeTopic(self._next_thread)

    async def send_message(
        self, chat_id=None, text=None, message_thread_id=None, **kwargs
    ):
        if message_thread_id in self.gone_threads:
            raise BadRequest("Message thread not found")
        self.sent.append((chat_id, text, message_thread_id))
        return SimpleNamespace(message_id=len(self.sent))


def _config():
    return SimpleNamespace(
        events=SimpleNamespace(chat_id=EVENTS_CHAT_ID),
        contexts={"dev": SimpleNamespace()},
    )


def _job_queue() -> MagicMock:
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []
    return job_queue


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def runner(db, bot):
    return ScheduleRunner(_config, bot, db, _job_queue())


@pytest.fixture
def dispatch_calls(monkeypatch):
    """Capture dispatch() calls; restores the registry afterwards.

    Fires the per-turn completion signal like the real agent loop does,
    so the runner's await returns instead of running out the timeout.
    """
    from open_shrimp.handlers.state import signal_turn_done

    calls: list[tuple[str, object, str | None]] = []

    async def _fake_dispatch(prompt, scope, placeholder=None):
        calls.append((prompt, scope, placeholder))
        signal_turn_done(scope)

    monkeypatch.setattr(dispatch_registry, "_dispatch_fn", _fake_dispatch)
    return calls


async def _make_task(db, *, schedule_type="interval", expr="30m", **overrides):
    return await create_scheduled_task(
        db,
        overrides.pop("context_name", "dev"),
        overrides.pop("name", "nightly"),
        overrides.pop("prompt", "check the build"),
        schedule_type,
        expr,
        **overrides,
    )


@pytest.mark.asyncio
async def test_run_creates_topic_and_dispatches(db, bot, runner, dispatch_calls):
    task = await _make_task(db)
    # A completed turn must release the runner promptly — long before the
    # task's 600s timeout (the persistent scope task never finishes, so
    # the runner must key off the per-turn signal, not the task).
    await asyncio.wait_for(runner._execute(task), timeout=5)
    assert not any("⏱️" in text for _, text, _ in bot.sent)

    # A ⏰ topic was created and mapped under the task's id-based key.
    assert bot.created_topics == ["⏰ nightly"]
    row = await get_event_topic(db, topic_key(task.id))
    assert row is not None
    thread_id = row[1]

    # Run-start note landed in the topic; the turn was dispatched there
    # with the task prompt wrapped in the trusted preamble.
    assert bot.sent[0][2] == thread_id
    assert "run starting" in bot.sent[0][1]
    assert len(dispatch_calls) == 1
    prompt, scope, _ = dispatch_calls[0]
    assert "check the build" in prompt
    assert 'Scheduled task "nightly" is firing' in prompt
    assert scope.chat_id == EVENTS_CHAT_ID
    assert scope.thread_id == thread_id


@pytest.mark.asyncio
async def test_topic_self_heal_after_deletion(db, bot, runner, dispatch_calls):
    task = await _make_task(db)
    # A stale mapping to a topic the user has deleted in Telegram.
    await set_event_topic(db, topic_key(task.id), EVENTS_CHAT_ID, 99)
    bot.gone_threads.add(99)

    await runner._execute(task)

    # The mapping was healed to a freshly created topic and the run
    # proceeded there.
    row = await get_event_topic(db, topic_key(task.id))
    assert row is not None and row[1] != 99
    new_thread = row[1]
    assert bot.created_topics == ["⏰ nightly"]
    assert bot.sent[-1][2] == new_thread
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][1].thread_id == new_thread


@pytest.mark.asyncio
async def test_overlap_skips_with_note(db, bot, runner, dispatch_calls):
    task = await _make_task(db)
    await set_event_topic(db, topic_key(task.id), EVENTS_CHAT_ID, 42)
    runner._running_ids.add(task.id)

    await runner._execute(task)

    assert dispatch_calls == []
    assert len(bot.sent) == 1
    chat_id, text, thread_id = bot.sent[0]
    assert text.startswith("⏭️")
    assert thread_id == 42


@pytest.mark.asyncio
async def test_overlap_skip_without_topic_is_silent(db, bot, runner, dispatch_calls):
    task = await _make_task(db)
    runner._running_ids.add(task.id)

    await runner._execute(task)

    assert dispatch_calls == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_timeout_cancels_turn_and_notes(db, bot, runner, monkeypatch):
    from open_shrimp.handlers import state

    task = await _make_task(db, timeout_seconds=0)
    turns: list[asyncio.Task] = []

    async def _fake_dispatch(prompt, scope, placeholder=None):
        turn = asyncio.create_task(asyncio.sleep(30))
        state._running_tasks[scope] = turn
        turns.append(turn)

    monkeypatch.setattr(dispatch_registry, "_dispatch_fn", _fake_dispatch)

    await runner._execute(task)

    assert len(turns) == 1
    assert turns[0].cancelled()
    assert any("⏱️" in text for _, text, _ in bot.sent)


@pytest.mark.asyncio
async def test_once_task_auto_deletes_after_run(db, bot, runner, dispatch_calls):
    task = await _make_task(
        db, schedule_type="once", expr="2099-01-01T00:00:00"
    )
    await runner._execute(task)

    assert len(dispatch_calls) == 1
    assert await get_all_scheduled_tasks(db) == []
    # The topic mapping survives as the run record's home.
    assert await get_event_topic(db, topic_key(task.id)) is not None


@pytest.mark.asyncio
async def test_missing_context_posts_note_no_dispatch(db, bot, runner, dispatch_calls):
    task = await _make_task(db, context_name="gone")
    await runner._execute(task)

    assert dispatch_calls == []
    assert len(bot.sent) == 1
    assert "no longer exists" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_context_added_after_startup_is_seen(db, bot, dispatch_calls):
    # The runner must resolve contexts through the live config, not a
    # construction-time snapshot: a hot-reloaded config that adds the
    # context makes the next firing run instead of skipping.
    config = _config()
    runner = ScheduleRunner(lambda: config, bot, db, _job_queue())
    task = await _make_task(db, context_name="hot-added")

    await runner._execute(task)
    assert dispatch_calls == []
    assert "no longer exists" in bot.sent[0][1]

    config = _config()
    config.contexts["hot-added"] = SimpleNamespace()
    await asyncio.wait_for(runner._execute(task), timeout=5)
    assert len(dispatch_calls) == 1


@pytest.mark.asyncio
async def test_migration_from_chat_bound_shape(tmp_path):
    path = tmp_path / "openshrimp.sqlite3"
    db = await aiosqlite.connect(path)
    await db.execute(
        """
        CREATE TABLE scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER NOT NULL DEFAULT 0,
            context_name TEXT NOT NULL,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_expr TEXT NOT NULL,
            timeout_seconds INTEGER NOT NULL DEFAULT 600,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            disabled INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat_id, message_thread_id, name)
        )
        """
    )
    rows = [
        # A disabled task migrates as enabled (topic deletion is no longer
        # a reason to stop firing).
        (1, 0, "dev", "daily", "p1", "interval", "1h", 600, 1),
        # Same name in another chat: old uniqueness was per-chat; the
        # first row per name wins.
        (2, 0, "dev", "daily", "p2", "interval", "2h", 600, 0),
        (1, 0, "dev", "other", "p3", "cron", "0 9 * * *", 300, 0),
    ]
    await db.executemany(
        "INSERT INTO scheduled_tasks "
        "(chat_id, message_thread_id, context_name, name, prompt, "
        " schedule_type, schedule_expr, timeout_seconds, disabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()
    await db.close()

    db = await init_db(path)
    try:
        cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "chat_id" not in columns
        assert "message_thread_id" not in columns
        assert "disabled" not in columns

        tasks = await get_all_scheduled_tasks(db)
        assert [(t.name, t.prompt) for t in tasks] == [
            ("daily", "p1"),
            ("other", "p3"),
        ]
        assert tasks[1].timeout_seconds == 300
    finally:
        await db.close()
