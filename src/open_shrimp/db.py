"""SQLite persistence for OpenShrimp.

Maps (chat_id, message_thread_id, context_name) -> session_id so sessions
can be resumed across bot restarts.  Forum topics (threads) get independent
sessions within the same chat.

Also stores scheduled tasks for the scheduler module.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import aiosqlite
from open_shrimp.paths import db_path as _default_db_path

logger = logging.getLogger(__name__)


class ChatScope(NamedTuple):
    """Identifies a unique conversation scope: a chat plus optional thread.

    ``thread_id`` is ``None`` for private chats and non-forum groups.
    The DB stores ``None`` as ``0``.
    """

    chat_id: int
    thread_id: int | None = None  # None for private/non-forum chats


def _thread_id_to_db(thread_id: int | None) -> int:
    """Convert a thread_id to the DB representation (0 for None)."""
    return thread_id if thread_id is not None else 0


_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL DEFAULT 0,
    context_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_thread_id, context_name)
)
"""

_CREATE_ACTIVE_CONTEXTS_TABLE = """
CREATE TABLE IF NOT EXISTS active_contexts (
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL DEFAULT 0,
    context_name TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_thread_id)
)
"""

_CREATE_PINNED_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS pinned_messages (
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_thread_id)
)
"""

_CREATE_ADDITIONAL_DIRECTORIES_TABLE = """
CREATE TABLE IF NOT EXISTS additional_directories (
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL DEFAULT 0,
    context_name TEXT NOT NULL,
    directory TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_thread_id, context_name, directory)
)
"""

_CREATE_SCHEDULED_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
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


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """Migrate old schema (no message_thread_id) to new schema.

    Detects the old schema by checking whether the sessions table has
    a message_thread_id column.  If not, creates new tables, copies
    rows with message_thread_id=0, and swaps.
    """
    cursor = await db.execute("PRAGMA table_info(sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "message_thread_id" in columns:
        return  # Already migrated

    logger.info("Migrating database schema to add message_thread_id...")

    # Create new tables with _new suffix
    await db.execute("""
        CREATE TABLE sessions_new (
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER NOT NULL DEFAULT 0,
            context_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_thread_id, context_name)
        )
    """)
    await db.execute("""
        CREATE TABLE active_contexts_new (
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER NOT NULL DEFAULT 0,
            context_name TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_thread_id)
        )
    """)
    await db.execute("""
        CREATE TABLE pinned_messages_new (
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_thread_id)
        )
    """)

    # Copy data with message_thread_id=0
    await db.execute(
        "INSERT INTO sessions_new (chat_id, message_thread_id, context_name, session_id) "
        "SELECT chat_id, 0, context_name, session_id FROM sessions"
    )
    await db.execute(
        "INSERT INTO active_contexts_new (chat_id, message_thread_id, context_name) "
        "SELECT chat_id, 0, context_name FROM active_contexts"
    )
    await db.execute(
        "INSERT INTO pinned_messages_new (chat_id, message_thread_id, message_id) "
        "SELECT chat_id, 0, message_id FROM pinned_messages"
    )

    # Swap tables
    await db.execute("DROP TABLE sessions")
    await db.execute("DROP TABLE active_contexts")
    await db.execute("DROP TABLE pinned_messages")
    await db.execute("ALTER TABLE sessions_new RENAME TO sessions")
    await db.execute("ALTER TABLE active_contexts_new RENAME TO active_contexts")
    await db.execute("ALTER TABLE pinned_messages_new RENAME TO pinned_messages")

    await db.commit()
    logger.info("Database schema migration complete.")


async def _migrate_scheduled_tasks_disabled(db: aiosqlite.Connection) -> None:
    """Add the ``disabled`` column to scheduled_tasks if it doesn't exist."""
    cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "disabled" in columns:
        return
    logger.info("Adding 'disabled' column to scheduled_tasks...")
    await db.execute(
        "ALTER TABLE scheduled_tasks ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
    )
    await db.commit()


async def init_db(db_path: Path | None = None) -> aiosqlite.Connection:
    """Create the database and tables, return the connection."""
    if db_path is None:
        db_path = _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    await db.execute(_CREATE_SESSIONS_TABLE)
    await db.execute(_CREATE_ACTIVE_CONTEXTS_TABLE)
    await db.execute(_CREATE_PINNED_MESSAGES_TABLE)
    await db.execute(_CREATE_SCHEDULED_TASKS_TABLE)
    await db.execute(_CREATE_ADDITIONAL_DIRECTORIES_TABLE)
    await db.commit()
    await _migrate_schema(db)
    await _migrate_scheduled_tasks_disabled(db)
    logger.info("Database initialized at %s", db_path)
    return db


async def get_session_id(
    db: aiosqlite.Connection, scope: ChatScope, context_name: str
) -> str | None:
    """Return the session_id for (scope, context_name), or None."""
    cursor = await db.execute(
        "SELECT session_id FROM sessions "
        "WHERE chat_id = ? AND message_thread_id = ? AND context_name = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_session_id(
    db: aiosqlite.Connection, scope: ChatScope, context_name: str, session_id: str
) -> None:
    """Insert or update the session_id for (scope, context_name)."""
    await db.execute(
        "INSERT INTO sessions (chat_id, message_thread_id, context_name, session_id) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (chat_id, message_thread_id, context_name) "
        "DO UPDATE SET session_id = excluded.session_id",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name, session_id),
    )
    await db.commit()


async def delete_session(
    db: aiosqlite.Connection, scope: ChatScope, context_name: str
) -> None:
    """Remove the session mapping for (scope, context_name)."""
    await db.execute(
        "DELETE FROM sessions "
        "WHERE chat_id = ? AND message_thread_id = ? AND context_name = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name),
    )
    await db.commit()


async def get_active_context(
    db: aiosqlite.Connection, scope: ChatScope
) -> str | None:
    """Return the active context name for a scope, or None if not set."""
    cursor = await db.execute(
        "SELECT context_name FROM active_contexts "
        "WHERE chat_id = ? AND message_thread_id = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id)),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_active_context(
    db: aiosqlite.Connection, scope: ChatScope, context_name: str
) -> None:
    """Insert or update the active context for a scope."""
    await db.execute(
        "INSERT INTO active_contexts (chat_id, message_thread_id, context_name) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (chat_id, message_thread_id) "
        "DO UPDATE SET context_name = excluded.context_name",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name),
    )
    await db.commit()


async def get_pinned_message_id(
    db: aiosqlite.Connection, scope: ChatScope
) -> int | None:
    """Return the pinned status message ID for a scope, or None."""
    cursor = await db.execute(
        "SELECT message_id FROM pinned_messages "
        "WHERE chat_id = ? AND message_thread_id = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id)),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_pinned_message_id(
    db: aiosqlite.Connection, scope: ChatScope, message_id: int
) -> None:
    """Insert or update the pinned status message ID for a scope."""
    await db.execute(
        "INSERT INTO pinned_messages (chat_id, message_thread_id, message_id) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (chat_id, message_thread_id) "
        "DO UPDATE SET message_id = excluded.message_id",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), message_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

# Maximum scheduled tasks per chat scope.
MAX_SCHEDULED_TASKS_PER_CHAT = 20


@dataclass
class ScheduledTask:
    """A scheduled task row from the database."""

    id: int
    chat_id: int
    message_thread_id: int
    context_name: str
    name: str
    prompt: str
    schedule_type: str  # "cron", "interval", "once"
    schedule_expr: str
    timeout_seconds: int
    created_at: str
    disabled: bool = False

    @property
    def scope(self) -> ChatScope:
        thread_id = self.message_thread_id if self.message_thread_id != 0 else None
        return ChatScope(chat_id=self.chat_id, thread_id=thread_id)


def _row_to_task(row: tuple) -> ScheduledTask:
    """Convert a DB row tuple to a ScheduledTask."""
    return ScheduledTask(
        id=row[0],
        chat_id=row[1],
        message_thread_id=row[2],
        context_name=row[3],
        name=row[4],
        prompt=row[5],
        schedule_type=row[6],
        schedule_expr=row[7],
        timeout_seconds=row[8],
        created_at=row[9],
        disabled=bool(row[10]),
    )


_SELECT_TASK_COLS = (
    "id, chat_id, message_thread_id, context_name, name, prompt, "
    "schedule_type, schedule_expr, timeout_seconds, created_at, disabled"
)


async def create_scheduled_task(
    db: aiosqlite.Connection,
    scope: ChatScope,
    context_name: str,
    name: str,
    prompt: str,
    schedule_type: str,
    schedule_expr: str,
    timeout_seconds: int = 600,
) -> ScheduledTask:
    """Insert a new scheduled task and return it.

    Raises:
        ValueError: If the max task limit per chat scope is reached.
        aiosqlite.IntegrityError: If a task with the same name already exists.
    """
    # Check per-scope task limit.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM scheduled_tasks "
        "WHERE chat_id = ? AND message_thread_id = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id)),
    )
    (count,) = await cursor.fetchone()  # type: ignore[misc]
    if count >= MAX_SCHEDULED_TASKS_PER_CHAT:
        raise ValueError(
            f"Maximum of {MAX_SCHEDULED_TASKS_PER_CHAT} scheduled tasks "
            f"per chat reached."
        )

    cursor = await db.execute(
        "INSERT INTO scheduled_tasks "
        "(chat_id, message_thread_id, context_name, name, prompt, "
        " schedule_type, schedule_expr, timeout_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.chat_id,
            _thread_id_to_db(scope.thread_id),
            context_name,
            name,
            prompt,
            schedule_type,
            schedule_expr,
            timeout_seconds,
        ),
    )
    await db.commit()
    task_id = cursor.lastrowid

    # Fetch the full row to return.
    cursor = await db.execute(
        f"SELECT {_SELECT_TASK_COLS} FROM scheduled_tasks WHERE id = ?",
        (task_id,),
    )
    row = await cursor.fetchone()
    return _row_to_task(row)  # type: ignore[arg-type]


async def delete_scheduled_task(
    db: aiosqlite.Connection,
    scope: ChatScope,
    name: str,
) -> bool:
    """Delete a scheduled task by name within a scope. Returns True if deleted."""
    cursor = await db.execute(
        "DELETE FROM scheduled_tasks "
        "WHERE chat_id = ? AND message_thread_id = ? AND name = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_scheduled_tasks(
    db: aiosqlite.Connection,
    scope: ChatScope,
) -> list[ScheduledTask]:
    """Return all scheduled tasks for a scope, ordered by creation time."""
    cursor = await db.execute(
        f"SELECT {_SELECT_TASK_COLS} FROM scheduled_tasks "
        "WHERE chat_id = ? AND message_thread_id = ? "
        "ORDER BY created_at",
        (scope.chat_id, _thread_id_to_db(scope.thread_id)),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def get_all_scheduled_tasks(
    db: aiosqlite.Connection,
    *,
    include_disabled: bool = False,
) -> list[ScheduledTask]:
    """Return all scheduled tasks across all scopes (for reload on startup)."""
    where = "" if include_disabled else " WHERE disabled = 0"
    cursor = await db.execute(
        f"SELECT {_SELECT_TASK_COLS} FROM scheduled_tasks{where} ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def delete_scheduled_task_by_id(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Delete a scheduled task by its primary key ID."""
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await db.commit()


async def disable_scheduled_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Mark a scheduled task as disabled."""
    await db.execute(
        "UPDATE scheduled_tasks SET disabled = 1 WHERE id = ?", (task_id,)
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Per-scope additional directories (runtime overrides via /add_dir)
# ---------------------------------------------------------------------------


async def add_additional_directory(
    db: aiosqlite.Connection,
    scope: ChatScope,
    context_name: str,
    directory: str,
) -> None:
    """Add a runtime additional directory for (scope, context_name)."""
    await db.execute(
        "INSERT OR IGNORE INTO additional_directories "
        "(chat_id, message_thread_id, context_name, directory) "
        "VALUES (?, ?, ?, ?)",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name, directory),
    )
    await db.commit()


async def remove_additional_directory(
    db: aiosqlite.Connection,
    scope: ChatScope,
    context_name: str,
    directory: str,
) -> bool:
    """Remove a runtime additional directory. Returns True if it existed."""
    cursor = await db.execute(
        "DELETE FROM additional_directories "
        "WHERE chat_id = ? AND message_thread_id = ? "
        "AND context_name = ? AND directory = ?",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name, directory),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_additional_directories(
    db: aiosqlite.Connection,
    scope: ChatScope,
    context_name: str,
) -> list[str]:
    """Return all runtime additional directories for (scope, context_name)."""
    cursor = await db.execute(
        "SELECT directory FROM additional_directories "
        "WHERE chat_id = ? AND message_thread_id = ? AND context_name = ? "
        "ORDER BY directory",
        (scope.chat_id, _thread_id_to_db(scope.thread_id), context_name),
    )
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_all_additional_directories_for_context(
    db: aiosqlite.Connection,
    context_name: str,
) -> list[str]:
    """Return the union of runtime additional directories across all scopes.

    Used for sandboxed contexts where the sandbox is shared across scopes.
    """
    cursor = await db.execute(
        "SELECT DISTINCT directory FROM additional_directories "
        "WHERE context_name = ? ORDER BY directory",
        (context_name,),
    )
    rows = await cursor.fetchall()
    return [row[0] for row in rows]
