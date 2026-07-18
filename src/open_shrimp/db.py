"""SQLite persistence for OpenShrimp.

Maps (chat_id, message_thread_id, context_name) -> session_id so sessions
can be resumed across bot restarts.  Forum topics (threads) get independent
sessions within the same chat.

Also stores scheduled tasks for the events schedule runner.
"""

import logging
import time
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

    @property
    def key(self) -> str:
        """Stable string key for cross-module bookkeeping (cache keys, etc.)."""
        if self.thread_id is None:
            return str(self.chat_id)
        return f"{self.chat_id}:{self.thread_id}"


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

_SCHEDULED_TASKS_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context_name TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_expr TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
"""

_CREATE_SCHEDULED_TASKS_TABLE = f"""
CREATE TABLE IF NOT EXISTS scheduled_tasks ({_SCHEDULED_TASKS_COLUMNS})
"""

_CREATE_SECURITY_KEY_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS security_key_sessions (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL DEFAULT 0,
    context_name TEXT NOT NULL,
    sandbox_id TEXT,
    device_id TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    approved_at INTEGER,
    ended_at INTEGER,
    end_reason TEXT
)
"""

_CREATE_ANDROID_COMPANION_INSTANCE_TABLE = """
CREATE TABLE IF NOT EXISTS android_companion_instance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    server_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
)
"""

_CREATE_ANDROID_COMPANION_PAIRING_CODES_TABLE = """
CREATE TABLE IF NOT EXISTS android_companion_pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    used_at INTEGER
)
"""

_CREATE_ANDROID_COMPANION_DEVICES_TABLE = """
CREATE TABLE IF NOT EXISTS android_companion_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    push_provider TEXT,
    push_token TEXT,
    push_endpoint TEXT,
    push_auth_secret TEXT,
    push_p256dh TEXT,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at INTEGER
)
"""

_CREATE_ANDROID_COMPANION_NONCES_TABLE = """
CREATE TABLE IF NOT EXISTS android_companion_nonces (
    device_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, nonce)
)
"""

_CREATE_EVENT_TOPICS_TABLE = """
CREATE TABLE IF NOT EXISTS event_topics (
    source TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL
)
"""

_CREATE_INBOUND_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS inbound_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    sender      TEXT,
    text        TEXT,
    raw         TEXT,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL,
    message_id  INTEGER,
    picked_up   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    reply_ref   TEXT,
    pickup_thread_id INTEGER,
    pending_notified INTEGER NOT NULL DEFAULT 0,
    context_ref TEXT
)
"""

_CREATE_INBOUND_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_events_source_created
    ON inbound_events (source, created_at)
"""

_CREATE_INBOUND_EVENTS_MESSAGE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_events_chat_message
    ON inbound_events (chat_id, message_id)
"""

_CREATE_INBOUND_EVENTS_PICKUP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_events_pickup
    ON inbound_events (chat_id, pickup_thread_id)
"""

_CREATE_MEETING_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at_ms INTEGER,
    duration_ms INTEGER,
    speaker_count INTEGER NOT NULL DEFAULT 0,
    word_count INTEGER NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL,
    state TEXT NOT NULL,
    notes_md TEXT,
    error TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE(device_id, meeting_id)
)
"""

_CREATE_SECURITY_KEY_AUDIT_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS security_key_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event TEXT NOT NULL,
    role TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL
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


async def _migrate_scheduled_tasks_events(db: aiosqlite.Connection) -> None:
    """Drop the chat binding (and ``disabled`` flag) from scheduled_tasks.

    Tasks run in dedicated per-task forum topics resolved through
    ``event_topics``, so the creating chat/thread is no longer stored.
    Previously-disabled rows migrate as enabled: a deleted topic is
    recreated on demand and is no longer a reason to stop firing.
    """
    cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "chat_id" not in columns:
        return
    logger.info("Migrating scheduled_tasks to the chat-unbound shape...")
    await db.execute(f"CREATE TABLE scheduled_tasks_new ({_SCHEDULED_TASKS_COLUMNS})")
    # The old table's uniqueness was per-chat, so the same name may exist in
    # several chats; keep the first row per name.
    await db.execute(
        "INSERT OR IGNORE INTO scheduled_tasks_new "
        "(id, context_name, name, prompt, schedule_type, schedule_expr, "
        " timeout_seconds, created_at) "
        "SELECT id, context_name, name, prompt, schedule_type, schedule_expr, "
        "       timeout_seconds, created_at "
        "FROM scheduled_tasks ORDER BY id"
    )
    await db.execute("DROP TABLE scheduled_tasks")
    await db.execute("ALTER TABLE scheduled_tasks_new RENAME TO scheduled_tasks")
    await db.commit()


async def _migrate_security_key_android_columns(db: aiosqlite.Connection) -> None:
    """Add Android companion columns to security_key_sessions when needed."""
    cursor = await db.execute("PRAGMA table_info(security_key_sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    migrations = {
        "requested_device_id": "ALTER TABLE security_key_sessions ADD COLUMN requested_device_id TEXT",
        "claimed_device_id": "ALTER TABLE security_key_sessions ADD COLUMN claimed_device_id TEXT",
        "push_sent_at": "ALTER TABLE security_key_sessions ADD COLUMN push_sent_at INTEGER",
        "push_status": "ALTER TABLE security_key_sessions ADD COLUMN push_status TEXT",
    }
    changed = False
    for column, sql in migrations.items():
        if column not in columns:
            await db.execute(sql)
            changed = True
    if changed:
        await db.commit()


async def _migrate_inbound_events_columns(db: aiosqlite.Connection) -> None:
    """Add reply routing / pickup-scope columns to inbound_events when needed."""
    cursor = await db.execute("PRAGMA table_info(inbound_events)")
    columns = {row[1] for row in await cursor.fetchall()}
    migrations = {
        "reply_ref": "ALTER TABLE inbound_events ADD COLUMN reply_ref TEXT",
        "pickup_thread_id": (
            "ALTER TABLE inbound_events ADD COLUMN pickup_thread_id INTEGER"
        ),
        "pending_notified": (
            "ALTER TABLE inbound_events ADD COLUMN "
            "pending_notified INTEGER NOT NULL DEFAULT 0"
        ),
        "context_ref": "ALTER TABLE inbound_events ADD COLUMN context_ref TEXT",
    }
    changed = False
    for column, sql in migrations.items():
        if column not in columns:
            await db.execute(sql)
            changed = True
    if changed:
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
    await db.execute(_CREATE_SECURITY_KEY_SESSIONS_TABLE)
    await db.execute(_CREATE_SECURITY_KEY_AUDIT_EVENTS_TABLE)
    await db.execute(_CREATE_ANDROID_COMPANION_INSTANCE_TABLE)
    await db.execute(_CREATE_ANDROID_COMPANION_PAIRING_CODES_TABLE)
    await db.execute(_CREATE_ANDROID_COMPANION_DEVICES_TABLE)
    await db.execute(_CREATE_ANDROID_COMPANION_NONCES_TABLE)
    await db.execute(_CREATE_EVENT_TOPICS_TABLE)
    await db.execute(_CREATE_INBOUND_EVENTS_TABLE)
    await db.execute(_CREATE_MEETING_JOBS_TABLE)
    await db.commit()
    await _migrate_schema(db)
    await _migrate_scheduled_tasks_events(db)
    await _migrate_security_key_android_columns(db)
    await _migrate_inbound_events_columns(db)
    # Index creation runs after the column migrations: the pickup index
    # references pickup_thread_id, which an upgraded old table only gains
    # via _migrate_inbound_events_columns.
    await db.execute(_CREATE_INBOUND_EVENTS_INDEX)
    await db.execute(_CREATE_INBOUND_EVENTS_MESSAGE_INDEX)
    await db.execute(_CREATE_INBOUND_EVENTS_PICKUP_INDEX)
    await db.commit()
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

# Maximum scheduled tasks (global — tasks are not bound to a chat).
MAX_SCHEDULED_TASKS = 20


@dataclass
class ScheduledTask:
    """A scheduled task row from the database."""

    id: int
    context_name: str
    name: str
    prompt: str
    schedule_type: str  # "cron", "interval", "once"
    schedule_expr: str
    timeout_seconds: int
    created_at: str


def _row_to_task(row: tuple) -> ScheduledTask:
    """Convert a DB row tuple to a ScheduledTask."""
    return ScheduledTask(
        id=row[0],
        context_name=row[1],
        name=row[2],
        prompt=row[3],
        schedule_type=row[4],
        schedule_expr=row[5],
        timeout_seconds=row[6],
        created_at=row[7],
    )


_SELECT_TASK_COLS = (
    "id, context_name, name, prompt, "
    "schedule_type, schedule_expr, timeout_seconds, created_at"
)


async def create_scheduled_task(
    db: aiosqlite.Connection,
    context_name: str,
    name: str,
    prompt: str,
    schedule_type: str,
    schedule_expr: str,
    timeout_seconds: int = 600,
) -> ScheduledTask:
    """Insert a new scheduled task and return it.

    Raises:
        ValueError: If the max task limit is reached.
        aiosqlite.IntegrityError: If a task with the same name already exists.
    """
    cursor = await db.execute("SELECT COUNT(*) FROM scheduled_tasks")
    (count,) = await cursor.fetchone()  # type: ignore[misc]
    if count >= MAX_SCHEDULED_TASKS:
        raise ValueError(
            f"Maximum of {MAX_SCHEDULED_TASKS} scheduled tasks reached."
        )

    cursor = await db.execute(
        "INSERT INTO scheduled_tasks "
        "(context_name, name, prompt, schedule_type, schedule_expr, "
        " timeout_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
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
    name: str,
) -> bool:
    """Delete a scheduled task by name. Returns True if deleted."""
    cursor = await db.execute(
        "DELETE FROM scheduled_tasks WHERE name = ?",
        (name,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_scheduled_tasks(
    db: aiosqlite.Connection,
) -> list[ScheduledTask]:
    """Return all scheduled tasks, ordered by creation time."""
    cursor = await db.execute(
        f"SELECT {_SELECT_TASK_COLS} FROM scheduled_tasks ORDER BY created_at"
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def get_all_scheduled_tasks(
    db: aiosqlite.Connection,
) -> list[ScheduledTask]:
    """Return all scheduled tasks (for reload on startup)."""
    cursor = await db.execute(
        f"SELECT {_SELECT_TASK_COLS} FROM scheduled_tasks ORDER BY id"
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


# ---------------------------------------------------------------------------
# Inbound event topics
# ---------------------------------------------------------------------------


async def get_event_topic(
    db: aiosqlite.Connection, source: str
) -> tuple[int, int] | None:
    """Return (chat_id, message_thread_id) for an event source, or None."""
    cursor = await db.execute(
        "SELECT chat_id, message_thread_id FROM event_topics WHERE source = ?",
        (source,),
    )
    row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def set_event_topic(
    db: aiosqlite.Connection, source: str, chat_id: int, thread_id: int
) -> None:
    """Insert or update the forum topic mapping for an event source."""
    await db.execute(
        "INSERT INTO event_topics (source, chat_id, message_thread_id) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (source) "
        "DO UPDATE SET chat_id = excluded.chat_id, "
        "message_thread_id = excluded.message_thread_id",
        (source, chat_id, thread_id),
    )
    await db.commit()


async def delete_event_topic(db: aiosqlite.Connection, source: str) -> None:
    """Remove the forum topic mapping for an event source."""
    await db.execute("DELETE FROM event_topics WHERE source = ?", (source,))
    await db.commit()


# ---------------------------------------------------------------------------
# Inbound events (pick-up handoff)
# ---------------------------------------------------------------------------


@dataclass
class InboundEvent:
    """A persisted inbound event row."""

    id: int
    source: str
    sender: str | None
    text: str | None
    raw: str | None  # json.dumps of the raw payload, for the JSON fallback
    chat_id: int
    thread_id: int
    message_id: int | None
    picked_up: bool
    created_at: int
    # JSON reply routing extracted by the source adapter at ingest time,
    # opaque to everything but the adapter's reply(); None if unroutable.
    reply_ref: str | None = None
    # The forum topic spawned by pick-up, if this event was picked up.
    pickup_thread_id: int | None = None
    # Set once the requester has been told their picked-up event is pending
    # the operator's response, so the notice fires at most once per event.
    pending_notified: bool = False
    # JSON handle for fetching surrounding context (thread history, etc.),
    # opaque to everything but the adapter's fetch_context(); None if the
    # source can't enrich.
    context_ref: str | None = None


async def insert_inbound_event(
    db: aiosqlite.Connection,
    *,
    source: str,
    sender: str | None,
    text: str | None,
    raw: str | None,
    chat_id: int,
    thread_id: int,
    reply_ref: str | None = None,
    context_ref: str | None = None,
) -> int:
    """Persist an inbound event, returning its integer id."""
    cursor = await db.execute(
        "INSERT INTO inbound_events "
        "(source, sender, text, raw, chat_id, thread_id, reply_ref, "
        "context_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, sender, text, raw, chat_id, thread_id, reply_ref,
         context_ref, int(time.time())),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def set_inbound_event_delivery(
    db: aiosqlite.Connection, event_id: int, thread_id: int, message_id: int
) -> None:
    """Record where the event message landed (button host, for later edits)."""
    await db.execute(
        "UPDATE inbound_events SET thread_id = ?, message_id = ? WHERE id = ?",
        (thread_id, message_id, event_id),
    )
    await db.commit()


_INBOUND_EVENT_COLUMNS = (
    "id, source, sender, text, raw, chat_id, thread_id, "
    "message_id, picked_up, created_at, reply_ref, pickup_thread_id, "
    "pending_notified, context_ref"
)


def _row_to_inbound_event(row: tuple) -> InboundEvent:
    return InboundEvent(
        id=row[0],
        source=row[1],
        sender=row[2],
        text=row[3],
        raw=row[4],
        chat_id=row[5],
        thread_id=row[6],
        message_id=row[7],
        picked_up=bool(row[8]),
        created_at=row[9],
        reply_ref=row[10],
        pickup_thread_id=row[11],
        pending_notified=bool(row[12]),
        context_ref=row[13],
    )


async def get_inbound_event(
    db: aiosqlite.Connection, event_id: int
) -> InboundEvent | None:
    """Return the inbound event row for *event_id*, or None."""
    cursor = await db.execute(
        f"SELECT {_INBOUND_EVENT_COLUMNS} FROM inbound_events WHERE id = ?",
        (event_id,),
    )
    row = await cursor.fetchone()
    return _row_to_inbound_event(row) if row else None


async def get_inbound_event_by_message(
    db: aiosqlite.Connection, chat_id: int, message_id: int
) -> InboundEvent | None:
    """Return the inbound event whose posted message is (chat_id, message_id)."""
    cursor = await db.execute(
        f"SELECT {_INBOUND_EVENT_COLUMNS} FROM inbound_events "
        f"WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    row = await cursor.fetchone()
    return _row_to_inbound_event(row) if row else None


async def get_inbound_event_by_pickup_scope(
    db: aiosqlite.Connection, chat_id: int, thread_id: int
) -> InboundEvent | None:
    """Return the event whose pick-up spawned the topic (chat_id, thread_id)."""
    cursor = await db.execute(
        f"SELECT {_INBOUND_EVENT_COLUMNS} FROM inbound_events "
        f"WHERE chat_id = ? AND pickup_thread_id = ?",
        (chat_id, thread_id),
    )
    row = await cursor.fetchone()
    return _row_to_inbound_event(row) if row else None


async def set_inbound_event_pickup_thread(
    db: aiosqlite.Connection, event_id: int, thread_id: int
) -> None:
    """Bind the event to the forum topic its pick-up spawned."""
    await db.execute(
        "UPDATE inbound_events SET pickup_thread_id = ? WHERE id = ?",
        (thread_id, event_id),
    )
    await db.commit()


async def mark_inbound_event_pending_notified(
    db: aiosqlite.Connection, event_id: int
) -> None:
    """Record that the requester was told their event is pending a response."""
    await db.execute(
        "UPDATE inbound_events SET pending_notified = 1 WHERE id = ?",
        (event_id,),
    )
    await db.commit()


async def claim_inbound_event(db: aiosqlite.Connection, event_id: int) -> bool:
    """Atomically claim an event for pick-up.

    Returns True if this call won the claim; False if the event was already
    picked up (double-tap / concurrent tap race gate).
    """
    cursor = await db.execute(
        "UPDATE inbound_events SET picked_up = 1 WHERE id = ? AND picked_up = 0",
        (event_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def release_inbound_event(db: aiosqlite.Connection, event_id: int) -> None:
    """Undo a claim so the pick-up button works again after a failed spawn."""
    await db.execute(
        "UPDATE inbound_events SET picked_up = 0 WHERE id = ?", (event_id,)
    )
    await db.commit()


async def prune_inbound_events(
    db: aiosqlite.Connection, source: str, keep: int = 500
) -> None:
    """Delete all but the newest *keep* events for a source."""
    await db.execute(
        "DELETE FROM inbound_events WHERE source = ? AND id NOT IN ("
        "SELECT id FROM inbound_events WHERE source = ? "
        "ORDER BY id DESC LIMIT ?)",
        (source, source, keep),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Meeting transcript jobs (Android companion upload -> notes -> delivery)
# ---------------------------------------------------------------------------


# Meeting-job state machine. The active set (still processing) and the
# terminal set must remain complementary: requeue-on-restart selects the
# former, completion-stamping selects the latter.
MEETING_STATE_RECEIVED = "received"
MEETING_STATE_GENERATING_NOTES = "generating_notes"
MEETING_STATE_DELIVERED = "delivered"
MEETING_STATE_FAILED = "failed"
_MEETING_ACTIVE_STATES = (MEETING_STATE_RECEIVED, MEETING_STATE_GENERATING_NOTES)
_MEETING_TERMINAL_STATES = (MEETING_STATE_DELIVERED, MEETING_STATE_FAILED)


@dataclass
class MeetingJob:
    """A persisted meeting-transcript job.

    States: received -> generating_notes -> delivered | failed.
    """

    id: int
    device_id: str
    meeting_id: str
    title: str
    started_at_ms: int | None
    duration_ms: int | None
    speaker_count: int
    word_count: int
    transcript: str
    state: str
    notes_md: str | None = None
    error: str | None = None
    created_at: int = 0
    completed_at: int | None = None


_MEETING_JOB_COLUMNS = (
    "id, device_id, meeting_id, title, started_at_ms, duration_ms, "
    "speaker_count, word_count, transcript, state, notes_md, error, "
    "created_at, completed_at"
)


def _row_to_meeting_job(row: tuple) -> MeetingJob:
    return MeetingJob(
        id=row[0],
        device_id=row[1],
        meeting_id=row[2],
        title=row[3],
        started_at_ms=row[4],
        duration_ms=row[5],
        speaker_count=row[6],
        word_count=row[7],
        transcript=row[8],
        state=row[9],
        notes_md=row[10],
        error=row[11],
        created_at=row[12],
        completed_at=row[13],
    )


async def upsert_meeting_job(
    db: aiosqlite.Connection,
    *,
    device_id: str,
    meeting_id: str,
    title: str,
    started_at_ms: int | None,
    duration_ms: int | None,
    speaker_count: int,
    word_count: int,
    transcript: str,
) -> int:
    """Insert or replace the job for (device, meeting); returns the row id.

    A re-upload (retry, or redone diarization) resets the job to ``received``
    and clears any previous notes/error so processing runs afresh.
    """
    cursor = await db.execute(
        "INSERT INTO meeting_jobs (device_id, meeting_id, title, "
        "started_at_ms, duration_ms, speaker_count, word_count, transcript, "
        "state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (device_id, meeting_id) DO UPDATE SET "
        "title = excluded.title, started_at_ms = excluded.started_at_ms, "
        "duration_ms = excluded.duration_ms, "
        "speaker_count = excluded.speaker_count, "
        "word_count = excluded.word_count, transcript = excluded.transcript, "
        "state = excluded.state, notes_md = NULL, error = NULL, "
        "completed_at = NULL, created_at = excluded.created_at "
        "RETURNING id",
        (
            device_id,
            meeting_id,
            title,
            started_at_ms,
            duration_ms,
            speaker_count,
            word_count,
            transcript,
            MEETING_STATE_RECEIVED,
            int(time.time()),
        ),
    )
    row = await cursor.fetchone()
    await db.commit()
    assert row is not None
    return row[0]


async def delete_meeting_job(
    db: aiosqlite.Connection, *, device_id: str, meeting_id: str
) -> bool:
    """Delete the job for (device, meeting); True if a row was removed."""
    cursor = await db.execute(
        "DELETE FROM meeting_jobs WHERE device_id = ? AND meeting_id = ?",
        (device_id, meeting_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_meeting_job(
    db: aiosqlite.Connection, job_id: int
) -> MeetingJob | None:
    """Return the meeting job row for *job_id*, or None."""
    cursor = await db.execute(
        f"SELECT {_MEETING_JOB_COLUMNS} FROM meeting_jobs WHERE id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    return _row_to_meeting_job(row) if row else None


async def get_unfinished_meeting_jobs(
    db: aiosqlite.Connection,
) -> list[MeetingJob]:
    """Jobs interrupted mid-processing (e.g. by a restart), oldest first."""
    placeholders = ", ".join("?" for _ in _MEETING_ACTIVE_STATES)
    cursor = await db.execute(
        f"SELECT {_MEETING_JOB_COLUMNS} FROM meeting_jobs "
        f"WHERE state IN ({placeholders}) ORDER BY id",
        _MEETING_ACTIVE_STATES,
    )
    rows = await cursor.fetchall()
    return [_row_to_meeting_job(row) for row in rows]


async def set_meeting_job_state(
    db: aiosqlite.Connection,
    job_id: int,
    state: str,
    *,
    notes_md: str | None = None,
    error: str | None = None,
) -> None:
    """Update a job's state; terminal states also stamp completed_at."""
    completed_at = int(time.time()) if state in _MEETING_TERMINAL_STATES else None
    await db.execute(
        "UPDATE meeting_jobs SET state = ?, "
        "notes_md = COALESCE(?, notes_md), error = ?, completed_at = ? "
        "WHERE id = ?",
        (state, notes_md, error, completed_at, job_id),
    )
    await db.commit()
