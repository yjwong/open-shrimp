"""SQLite session ID mapping for OpenUdang.

Maps (chat_id, message_thread_id, context_name) -> session_id so sessions
can be resumed across bot restarts.  Forum topics (threads) get independent
sessions within the same chat.
"""

import logging
from pathlib import Path
from typing import NamedTuple

import aiosqlite

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


DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "openudang" / "sessions.db"

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


async def init_db(db_path: Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """Create the database and tables, return the connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    await db.execute(_CREATE_SESSIONS_TABLE)
    await db.execute(_CREATE_ACTIVE_CONTEXTS_TABLE)
    await db.execute(_CREATE_PINNED_MESSAGES_TABLE)
    await db.commit()
    await _migrate_schema(db)
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
