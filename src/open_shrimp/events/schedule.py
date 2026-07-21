"""Scheduled tasks: cron/interval/once Claude prompts in per-task topics.

Each task owns a durable forum topic (``⏰ <name>``) in the events chat,
resolved through the same ``event_topics`` machinery as the inbox topics —
recreated on demand if deleted, never a fatal error.  Each firing is a
normal agent turn dispatched into that topic through ``dispatch_registry``:
full context tool policy, approval keyboards, streaming output, and the
user can reply in the topic to interact with the run.

Data flow:
  CREATE:
    Claude ──▶ MCP create_schedule ──▶ validate ──▶ DB INSERT ──▶ JobQueue

  EXECUTE:
    APScheduler fires ──▶ busy/capacity check (skip + ⏭️ note)
      ──▶ resolve task topic (self-heal if deleted) ──▶ reset scope
      ──▶ set task context ──▶ dispatch ──▶ await turn with timeout

  RELOAD (bot startup, via EventManager.start()):
    DB SELECT * ──▶ for each task: register with JobQueue (skip stale)
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from telegram import Bot
from telegram.error import BadRequest
from telegram.ext import JobQueue

from open_shrimp.config import Config
from open_shrimp.db import (
    ChatScope,
    ScheduledTask,
    delete_event_topic,
    delete_scheduled_task_by_id,
    get_all_scheduled_tasks,
    get_event_topic,
    set_active_context,
)
from open_shrimp.telegram_topics import is_topic_gone, resolve_or_create_topic

logger = logging.getLogger(__name__)

# Maximum concurrent scheduled task executions.
_MAX_CONCURRENT_TASKS = 3

# Minimum interval for recurring tasks (seconds).
_MIN_INTERVAL_SECONDS = 300  # 5 minutes

# The runner currently running with the bot, if any.  Set on start() and
# cleared on stop() so the scheduling tools can reach the live runner
# without threading it through the tool wiring.
_active_runner: "ScheduleRunner | None" = None


def get_active_runner() -> "ScheduleRunner | None":
    return _active_runner


def topic_key(task_id: int) -> str:
    """The ``event_topics`` key for a task's durable topic (id, not name)."""
    return f"schedule:{task_id}"


def _topic_name(task: ScheduledTask) -> str:
    return f"⏰ {task.name}"[:128]


def _job_name(task_id: int) -> str:
    return f"scheduled_task_{task_id}"


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

# Matches interval strings like "30m", "1h", "2d", "90s".
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)

_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval_seconds(expr: str) -> int:
    """Parse an interval expression like '30m' into seconds.

    Raises ValueError if the expression is invalid.
    """
    m = _INTERVAL_RE.match(expr.strip())
    if not m:
        raise ValueError(
            f"Invalid interval expression: {expr!r}. "
            f"Expected format like '30m', '1h', '2d', '90s'."
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    seconds = value * _INTERVAL_MULTIPLIERS[unit]
    if seconds <= 0:
        raise ValueError("Interval must be positive.")
    return seconds


def validate_schedule(schedule_type: str, schedule_expr: str) -> None:
    """Validate a schedule type and expression.

    Raises ValueError with a user-friendly message if invalid.
    """
    if schedule_type not in ("cron", "interval", "once"):
        raise ValueError(
            f"Invalid schedule_type: {schedule_expr!r}. "
            f"Must be 'cron', 'interval', or 'once'."
        )

    if schedule_type == "interval":
        seconds = parse_interval_seconds(schedule_expr)
        if seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"Minimum interval is {_MIN_INTERVAL_SECONDS // 60} minutes. "
                f"Got {seconds} seconds."
            )

    elif schedule_type == "cron":
        # Validate cron by trying to construct an APScheduler CronTrigger.
        from apscheduler.triggers.cron import CronTrigger

        parts = schedule_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields "
                f"(minute hour day month day_of_week). Got {len(parts)} fields."
            )
        try:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc

        # Check minimum interval for cron: reject "* * * * *" (every minute)
        # by checking if the trigger would fire within _MIN_INTERVAL_SECONDS.
        now = datetime.now(timezone.utc)
        first = trigger.get_next_fire_time(None, now)
        if first is not None:
            second = trigger.get_next_fire_time(first, first)
            if second is not None:
                gap = (second - first).total_seconds()
                if gap < _MIN_INTERVAL_SECONDS:
                    raise ValueError(
                        f"Cron fires too frequently ({gap:.0f}s between runs). "
                        f"Minimum is {_MIN_INTERVAL_SECONDS // 60} minutes."
                    )

    elif schedule_type == "once":
        try:
            dt = datetime.fromisoformat(schedule_expr)
            if dt.tzinfo is None:
                # Treat naive datetimes as UTC.
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError(
                f"Invalid datetime for one-shot schedule: {schedule_expr!r}. "
                f"Expected ISO 8601 format (e.g. '2026-03-21T09:00:00'). {exc}"
            ) from exc


def _format_duration(seconds: int) -> str:
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class ScheduleRunner:
    """Owns scheduled-task registration and execution.

    Started/stopped by :class:`~open_shrimp.events.manager.EventManager`
    alongside the source adapters.  Firings are dispatches into per-task
    topics, not inbox posts — the runner never goes through the sink.
    """

    def __init__(
        self,
        get_config: Callable[[], Config],
        bot: Bot,
        db: aiosqlite.Connection,
        job_queue: JobQueue,
    ) -> None:
        events = get_config().events
        assert events is not None, "ScheduleRunner requires configured events"
        # A getter, not a snapshot: the config hot-reloads while the runner
        # lives, and context checks must see contexts added since startup.
        self._get_config = get_config
        self._bot = bot
        self._db = db
        self._job_queue = job_queue
        self._chat_id = events.chat_id
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TASKS)
        # Currently-executing task IDs, to enforce max_instances=1.
        self._running_ids: set[int] = set()

    async def start(self) -> None:
        global _active_runner
        _active_runner = self
        await self._reload_tasks()

    async def stop(self) -> None:
        global _active_runner
        if _active_runner is self:
            _active_runner = None

    # -- Registration -------------------------------------------------------

    def register_task(self, task: ScheduledTask) -> bool:
        """Register *task* with the JobQueue.

        Returns True if successfully registered, False if skipped.
        """
        # Remove existing job with this name (in case of re-registration).
        self.unregister_task(task.id)

        async def _job_callback(context: Any) -> None:
            await self._execute(task)

        try:
            if task.schedule_type == "interval":
                seconds = parse_interval_seconds(task.schedule_expr)
                self._job_queue.run_repeating(
                    _job_callback,
                    interval=seconds,
                    first=seconds,  # First fire after one interval, not immediately.
                    name=_job_name(task.id),
                )

            elif task.schedule_type == "cron":
                from apscheduler.triggers.cron import CronTrigger

                parts = task.schedule_expr.strip().split()
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
                # Use run_custom to register cron jobs through PTB's JobQueue
                # wrapper.  Direct scheduler.add_job() bypasses PTB's args
                # wrapping, which causes get_jobs_by_name() to crash with
                # "tuple index out of range" when it calls from_aps_job().
                self._job_queue.run_custom(
                    _job_callback,
                    job_kwargs={"trigger": trigger},
                    name=_job_name(task.id),
                )

            elif task.schedule_type == "once":
                dt = datetime.fromisoformat(task.schedule_expr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)

                # Skip one-shot tasks whose time has passed.
                if dt <= datetime.now(timezone.utc):
                    logger.info(
                        "Skipping past one-shot task %d (%s): %s",
                        task.id,
                        task.name,
                        task.schedule_expr,
                    )
                    return False

                self._job_queue.run_once(
                    _job_callback,
                    when=dt,
                    name=_job_name(task.id),
                )

            else:
                logger.warning(
                    "Unknown schedule_type %r for task %d",
                    task.schedule_type,
                    task.id,
                )
                return False

        except Exception:
            logger.exception("Failed to register task %d (%s)", task.id, task.name)
            return False

        logger.info(
            "Registered scheduled task %d: %s (%s %s)",
            task.id,
            task.name,
            task.schedule_type,
            task.schedule_expr,
        )
        return True

    def unregister_task(self, task_id: int) -> None:
        """Remove the task's job from the JobQueue so it stops firing."""
        for job in self._job_queue.get_jobs_by_name(_job_name(task_id)):
            job.schedule_removal()

    async def _reload_tasks(self) -> int:
        """Load all scheduled tasks from DB and register with the JobQueue.

        Called once on start.  Returns the number of tasks registered.
        Stale one-shot tasks (datetime in the past) are deleted from the
        DB.  Tasks with missing contexts are still registered — the
        context may be hot-added to the config later, and each firing
        re-checks against the live config anyway.
        """
        tasks = await get_all_scheduled_tasks(self._db)
        registered = 0

        for task in tasks:
            if task.context_name not in self._get_config().contexts:
                logger.warning(
                    "Task %d (%s): context %r not in config; firings "
                    "will skip until it is added",
                    task.id,
                    task.name,
                    task.context_name,
                )

            # Delete stale one-shot tasks.
            if task.schedule_type == "once":
                try:
                    dt = datetime.fromisoformat(task.schedule_expr)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                    if dt <= datetime.now(timezone.utc):
                        await delete_scheduled_task_by_id(self._db, task.id)
                        logger.info(
                            "Deleted stale one-shot task %d (%s)",
                            task.id,
                            task.name,
                        )
                        continue
                except ValueError:
                    logger.warning("Invalid datetime for task %d, deleting", task.id)
                    await delete_scheduled_task_by_id(self._db, task.id)
                    continue

            if self.register_task(task):
                registered += 1

        logger.info("Reloaded %d scheduled tasks from database", registered)
        return registered

    # -- Execution ----------------------------------------------------------

    async def _execute(self, task: ScheduledTask) -> None:
        """Run one firing of *task*; never raises.

        Overlap and capacity are checked before topic resolution; a
        skipped firing posts a best-effort ⏭️ note into the task topic
        (if one already exists).
        """
        if task.id in self._running_ids:
            await self._post_skip_note(task, "previous run still going")
            return
        if self._semaphore.locked():
            await self._post_skip_note(task, "at concurrent-run capacity")
            return

        self._running_ids.add(task.id)
        try:
            async with self._semaphore:
                await self._run_once(task)
        except Exception:
            logger.exception(
                "Scheduled task %d (%s) failed", task.id, task.name
            )
        finally:
            self._running_ids.discard(task.id)

        # Auto-delete one-shot tasks after execution; the topic remains
        # as a record.
        if task.schedule_type == "once":
            try:
                await delete_scheduled_task_by_id(self._db, task.id)
                logger.info("Auto-deleted one-shot task %d (%s)", task.id, task.name)
            except Exception:
                logger.debug("Failed to auto-delete one-shot task %d", task.id)

    async def _post_skip_note(self, task: ScheduledTask, reason: str) -> None:
        """Best-effort ⏭️ note into an already-existing task topic."""
        logger.info("Skipping task %d (%s): %s", task.id, task.name, reason)
        row = await get_event_topic(self._db, topic_key(task.id))
        if row is None:
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=f"⏭️ Skipped {task.name}: {reason}.",
                message_thread_id=row[1],
            )
        except Exception:
            logger.debug(
                "Failed to post skip note for task %d", task.id, exc_info=True
            )

    async def _send_run_message(self, task: ScheduledTask, text: str) -> int:
        """Resolve the task topic and post *text* into it.

        If the topic was deleted since the last run, the stale mapping is
        dropped and the topic recreated, retrying the send exactly once —
        the same self-heal as the event sink.  Returns the thread id.
        """
        key = topic_key(task.id)
        thread_id = await resolve_or_create_topic(
            self._bot,
            self._db,
            key=key,
            chat_id=self._chat_id,
            name=_topic_name(task),
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, message_thread_id=thread_id
            )
        except BadRequest as exc:
            if not is_topic_gone(exc):
                raise
            logger.info(
                "Task topic for %d (%s) is gone (%s); recreating",
                task.id,
                task.name,
                exc,
            )
            await delete_event_topic(self._db, key)
            thread_id = await resolve_or_create_topic(
                self._bot,
                self._db,
                key=key,
                chat_id=self._chat_id,
                name=_topic_name(task),
            )
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, message_thread_id=thread_id
            )
        return thread_id

    async def _run_once(self, task: ScheduledTask) -> None:
        """One firing, under the semaphore: full interactive turn in the topic."""
        from open_shrimp.db import get_active_context
        from open_shrimp.dispatch_registry import dispatch
        from open_shrimp.handlers.state import (
            arm_turn_done,
            disarm_turn_done,
            reset_scope,
        )
        from open_shrimp.handlers.utils import _cancel_running

        if task.context_name not in self._get_config().contexts:
            logger.warning(
                "Scheduled task %d (%s): context %r not found, skipping",
                task.id,
                task.name,
                task.context_name,
            )
            try:
                await self._send_run_message(
                    task,
                    f"⚠️ Scheduled task {task.name!r} skipped: context "
                    f"{task.context_name!r} no longer exists.",
                )
            except Exception:
                logger.debug(
                    "Failed to post context-missing note for task %d",
                    task.id,
                    exc_info=True,
                )
            return

        thread_id = await self._send_run_message(
            task, f"⏰ {task.name} · run starting"
        )
        scope = ChatScope(chat_id=self._chat_id, thread_id=thread_id)

        logger.info(
            "Executing scheduled task %d (%s) in context %s",
            task.id,
            task.name,
            task.context_name,
        )

        # Fresh session per run: the topic keeps visible history, the agent
        # starts clean.  Reset under the scope's current context (a manual
        # /context in the topic may have changed it), then rebind the
        # task's own context before dispatching — same ordering as pick-up.
        current_ctx = await get_active_context(self._db, scope) or task.context_name
        await reset_scope(scope, current_ctx, self._db)
        await set_active_context(self._db, scope, task.context_name)

        # The prompt is trusted text: authored by the allowlisted user at
        # create time, nothing provider-delivered involved.
        prompt = (
            f'Scheduled task "{task.name}" is firing (automated run — no '
            f"human is watching live, but output lands in this topic). "
            f"{task.prompt}"
        )
        # The scope's asyncio task is a poor completion signal — the
        # persistent client keeps it alive across turns — so the agent
        # loop fires a per-turn event instead.  Armed before dispatch so
        # even an instantly-finishing turn cannot be missed; the loop's
        # teardown path also fires it, covering crashed turns.
        turn_done = arm_turn_done(scope)
        try:
            await dispatch(prompt, self._chat_id, thread_id=thread_id)
            try:
                await asyncio.wait_for(
                    turn_done.wait(), timeout=task.timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Scheduled task %d (%s) timed out after %ds",
                    task.id,
                    task.name,
                    task.timeout_seconds,
                )
                await _cancel_running(scope)
                try:
                    await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=(
                            f"⏱️ Timed out after "
                            f"{_format_duration(task.timeout_seconds)}."
                        ),
                        message_thread_id=thread_id,
                    )
                except Exception:
                    logger.debug(
                        "Failed to post timeout note for task %d",
                        task.id,
                        exc_info=True,
                    )
            else:
                logger.info(
                    "Scheduled task %d (%s) completed", task.id, task.name
                )
        finally:
            disarm_turn_done(scope, turn_done)
