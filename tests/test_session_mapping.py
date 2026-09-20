"""The persisted session mapping is keyed by backend.

Session ids are minted by the backend that ran the turn and mean nothing to
the other one: hand OpenCode's ``ses_...`` to ``claude --resume`` and the CLI
rejects it as neither a UUID nor a session title.  So each backend gets its
own row per (scope, context), and a topic that switches back and forth keeps
both histories.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from open_shrimp.db import (
    ChatScope,
    delete_session,
    get_session_id,
    init_db,
    set_session_id,
)

pytestmark = pytest.mark.asyncio

SCOPE = ChatScope(chat_id=5, thread_id=77)
CLAUDE_ID = "4f4b1c6e-0f7a-4a1e-9a3e-2c9d7b5e1a20"
OPENCODE_ID = "ses_f422d9124ffekIahBaWuP8fQjO"


async def test_each_backend_keeps_its_own_session(tmp_path: Path):
    db = await init_db(tmp_path / "openshrimp.sqlite3")
    try:
        await set_session_id(db, SCOPE, "labs", "claude_sdk", CLAUDE_ID)
        await set_session_id(db, SCOPE, "labs", "opencode", OPENCODE_ID)

        assert await get_session_id(db, SCOPE, "labs", "claude_sdk") == CLAUDE_ID
        assert await get_session_id(db, SCOPE, "labs", "opencode") == OPENCODE_ID
    finally:
        await db.close()


async def test_a_backend_with_no_session_here_reads_as_none(tmp_path: Path):
    """The read that used to hand a foreign id to the wrong CLI."""
    db = await init_db(tmp_path / "openshrimp.sqlite3")
    try:
        await set_session_id(db, SCOPE, "labs", "opencode", OPENCODE_ID)
        assert await get_session_id(db, SCOPE, "labs", "claude_sdk") is None
    finally:
        await db.close()


async def test_delete_without_a_backend_clears_every_one(tmp_path: Path):
    """``/clear`` means the topic keeps no history on either binary."""
    db = await init_db(tmp_path / "openshrimp.sqlite3")
    try:
        await set_session_id(db, SCOPE, "labs", "claude_sdk", CLAUDE_ID)
        await set_session_id(db, SCOPE, "labs", "opencode", OPENCODE_ID)

        await delete_session(db, SCOPE, "labs")

        assert await get_session_id(db, SCOPE, "labs", "claude_sdk") is None
        assert await get_session_id(db, SCOPE, "labs", "opencode") is None
    finally:
        await db.close()


async def test_delete_naming_a_backend_spares_the_other(tmp_path: Path):
    db = await init_db(tmp_path / "openshrimp.sqlite3")
    try:
        await set_session_id(db, SCOPE, "labs", "claude_sdk", CLAUDE_ID)
        await set_session_id(db, SCOPE, "labs", "opencode", OPENCODE_ID)

        await delete_session(db, SCOPE, "labs", "opencode")

        assert await get_session_id(db, SCOPE, "labs", "claude_sdk") == CLAUDE_ID
        assert await get_session_id(db, SCOPE, "labs", "opencode") is None
    finally:
        await db.close()


async def _legacy_db(path: Path) -> None:
    """A sessions table from before the backend column existed."""
    db = await aiosqlite.connect(path)
    await db.execute(
        "CREATE TABLE sessions ("
        " chat_id INTEGER NOT NULL,"
        " message_thread_id INTEGER NOT NULL DEFAULT 0,"
        " context_name TEXT NOT NULL,"
        " session_id TEXT NOT NULL,"
        " PRIMARY KEY (chat_id, message_thread_id, context_name))"
    )
    await db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)",
        (SCOPE.chat_id, SCOPE.thread_id, "labs", CLAUDE_ID),
    )
    await db.commit()
    await db.close()


async def test_legacy_rows_land_on_the_configured_default(tmp_path: Path):
    """Pre-column rows name no backend, and the id alone cannot say which.

    They are stamped with the top-level ``backend:``, which served every
    context that did not override it.
    """
    path = tmp_path / "openshrimp.sqlite3"
    await _legacy_db(path)

    db = await init_db(path, default_backend="claude_sdk")
    try:
        assert await get_session_id(db, SCOPE, "labs", "claude_sdk") == CLAUDE_ID
        assert await get_session_id(db, SCOPE, "labs", "opencode") is None
    finally:
        await db.close()


async def test_legacy_rows_go_when_there_is_no_default_to_stamp(tmp_path: Path):
    path = tmp_path / "openshrimp.sqlite3"
    await _legacy_db(path)

    db = await init_db(path)
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM sessions")
        assert (await cursor.fetchone())[0] == 0
        # The table still has the widened shape, so writes work afterwards.
        await set_session_id(db, SCOPE, "labs", "opencode", OPENCODE_ID)
        assert await get_session_id(db, SCOPE, "labs", "opencode") == OPENCODE_ID
    finally:
        await db.close()
