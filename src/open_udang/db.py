"""SQLite session ID mapping for OpenUdang.

Maps (chat_id, context_name) -> session_id so sessions can be resumed
across bot restarts.
"""

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "openudang" / "sessions.db"

_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER NOT NULL,
    context_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, context_name)
)
"""

_CREATE_ACTIVE_CONTEXTS_TABLE = """
CREATE TABLE IF NOT EXISTS active_contexts (
    chat_id INTEGER PRIMARY KEY,
    context_name TEXT NOT NULL
)
"""

_CREATE_PINNED_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS pinned_messages (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL
)
"""


async def init_db(db_path: Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """Create the database and tables, return the connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    await db.execute(_CREATE_SESSIONS_TABLE)
    await db.execute(_CREATE_ACTIVE_CONTEXTS_TABLE)
    await db.execute(_CREATE_PINNED_MESSAGES_TABLE)
    await db.commit()
    logger.info("Database initialized at %s", db_path)
    return db


async def get_session_id(
    db: aiosqlite.Connection, chat_id: int, context_name: str
) -> str | None:
    """Return the session_id for (chat_id, context_name), or None."""
    cursor = await db.execute(
        "SELECT session_id FROM sessions WHERE chat_id = ? AND context_name = ?",
        (chat_id, context_name),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_session_id(
    db: aiosqlite.Connection, chat_id: int, context_name: str, session_id: str
) -> None:
    """Insert or update the session_id for (chat_id, context_name)."""
    await db.execute(
        "INSERT INTO sessions (chat_id, context_name, session_id) VALUES (?, ?, ?) "
        "ON CONFLICT (chat_id, context_name) DO UPDATE SET session_id = excluded.session_id",
        (chat_id, context_name, session_id),
    )
    await db.commit()


async def delete_session(
    db: aiosqlite.Connection, chat_id: int, context_name: str
) -> None:
    """Remove the session mapping for (chat_id, context_name)."""
    await db.execute(
        "DELETE FROM sessions WHERE chat_id = ? AND context_name = ?",
        (chat_id, context_name),
    )
    await db.commit()


async def get_active_context(
    db: aiosqlite.Connection, chat_id: int
) -> str | None:
    """Return the active context name for a chat, or None if not set."""
    cursor = await db.execute(
        "SELECT context_name FROM active_contexts WHERE chat_id = ?",
        (chat_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_active_context(
    db: aiosqlite.Connection, chat_id: int, context_name: str
) -> None:
    """Insert or update the active context for a chat."""
    await db.execute(
        "INSERT INTO active_contexts (chat_id, context_name) VALUES (?, ?) "
        "ON CONFLICT (chat_id) DO UPDATE SET context_name = excluded.context_name",
        (chat_id, context_name),
    )
    await db.commit()


async def get_pinned_message_id(
    db: aiosqlite.Connection, chat_id: int
) -> int | None:
    """Return the pinned status message ID for a chat, or None."""
    cursor = await db.execute(
        "SELECT message_id FROM pinned_messages WHERE chat_id = ?",
        (chat_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_pinned_message_id(
    db: aiosqlite.Connection, chat_id: int, message_id: int
) -> None:
    """Insert or update the pinned status message ID for a chat."""
    await db.execute(
        "INSERT INTO pinned_messages (chat_id, message_id) VALUES (?, ?) "
        "ON CONFLICT (chat_id) DO UPDATE SET message_id = excluded.message_id",
        (chat_id, message_id),
    )
    await db.commit()
