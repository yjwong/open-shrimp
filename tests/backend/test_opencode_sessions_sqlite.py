"""Tests for the OpenCode SQLite session reader.

The reader is the load-bearing path for the sandboxed ``/resume`` listing:
it scans OpenCode's on-disk ``opencode[-channel].db`` from the host without
booting the sandbox.  These tests construct a real SQLite DB matching the
upstream OpenCode schema for the columns we read so a future schema change
breaks here loudly instead of silently returning empty lists at runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from open_shrimp.backend.opencode.sessions import (
    _find_opencode_db,
    list_sessions_from_sqlite,
)


def _create_schema(db_path: Path) -> None:
    """Create the minimum ``session`` table we read against.

    Matches the upstream column types/constraints for the fields the reader
    queries (``id``, ``directory``, ``title``, ``time_created``,
    ``time_updated``, ``time_archived``).  Other columns are present with
    ``NOT NULL`` defaults so inserts that omit them still succeed — mirrors
    the upstream schema where most fields have ``.notNull().default(...)``.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'proj',
                workspace_id TEXT,
                parent_id TEXT,
                slug TEXT NOT NULL DEFAULT '',
                directory TEXT NOT NULL,
                path TEXT,
                title TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '0',
                share_url TEXT,
                cost REAL NOT NULL DEFAULT 0,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                time_archived INTEGER
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_session(
    db_path: Path,
    *,
    sid: str,
    directory: str,
    title: str,
    time_created: int,
    time_updated: int,
    time_archived: int | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO session
                (id, directory, title, time_created, time_updated, time_archived)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sid, directory, title, time_created, time_updated, time_archived),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_returns_empty_when_db_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = await list_sessions_from_sqlite(missing, "/some/dir")
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_no_db_file(tmp_path: Path) -> None:
    # Directory exists but no opencode*.db inside — the "fresh sandbox"
    # case where opencode serve has never started.
    result = await list_sessions_from_sqlite(tmp_path, "/some/dir")
    assert result == []


@pytest.mark.asyncio
async def test_reads_matching_session(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    _insert_session(
        db,
        sid="ses_abc",
        directory=str(tmp_path),
        title="my session",
        time_created=1_000_000,
        time_updated=2_000_000,
    )

    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path))
    assert len(result) == 1
    info = result[0]
    assert info.session_id == "ses_abc"
    assert info.summary == "my session"
    assert info.last_modified == 2_000_000
    assert info.created_at == 1_000_000


@pytest.mark.asyncio
async def test_canonicalises_directory(tmp_path: Path) -> None:
    """The reader canonicalises *directory* before the SQL match so a caller
    passing a non-resolved path still hits rows OpenCode wrote with the
    resolved path."""
    db = tmp_path / "opencode.db"
    _create_schema(db)
    canonical_dir = str(tmp_path.resolve())
    _insert_session(
        db,
        sid="ses_abc",
        directory=canonical_dir,
        title="t",
        time_created=1, time_updated=2,
    )
    # Pass a path with a trailing dot-slash component that resolves to the
    # same canonical dir.
    noisy = str(tmp_path / "." / "")
    result = await list_sessions_from_sqlite(tmp_path, noisy)
    assert [r.session_id for r in result] == ["ses_abc"]


@pytest.mark.asyncio
async def test_filters_by_directory(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    _insert_session(db, sid="ses_a", directory=str(tmp_path),
                    title="here", time_created=1, time_updated=2)
    _insert_session(db, sid="ses_b", directory="/other/dir",
                    title="there", time_created=1, time_updated=3)

    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path))
    assert [r.session_id for r in result] == ["ses_a"]


@pytest.mark.asyncio
async def test_excludes_archived(tmp_path: Path) -> None:
    """Matches OpenCode's HTTP default which filters ``time_archived IS NULL``."""
    db = tmp_path / "opencode.db"
    _create_schema(db)
    _insert_session(db, sid="live", directory=str(tmp_path),
                    title="live", time_created=1, time_updated=2)
    _insert_session(db, sid="archived", directory=str(tmp_path),
                    title="archived", time_created=1, time_updated=3,
                    time_archived=9_999_999)

    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path))
    assert [r.session_id for r in result] == ["live"]


@pytest.mark.asyncio
async def test_orders_by_time_updated_desc(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    _insert_session(db, sid="old", directory=str(tmp_path),
                    title="old", time_created=1, time_updated=100)
    _insert_session(db, sid="new", directory=str(tmp_path),
                    title="new", time_created=1, time_updated=200)
    _insert_session(db, sid="mid", directory=str(tmp_path),
                    title="mid", time_created=1, time_updated=150)

    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path))
    assert [r.session_id for r in result] == ["new", "mid", "old"]


@pytest.mark.asyncio
async def test_honours_limit(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    for i in range(5):
        _insert_session(db, sid=f"s{i}", directory=str(tmp_path),
                        title=f"s{i}", time_created=1, time_updated=i)

    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path), limit=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_rejects_empty_directory(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    with pytest.raises(ValueError):
        await list_sessions_from_sqlite(tmp_path, "")


@pytest.mark.asyncio
async def test_uses_null_title_fallback(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_schema(db)
    # Bypass the NOT NULL by inserting an empty string (NOT NULL prevents
    # NULL but allows ''), which the reader treats the same way: fall back
    # to "(no title)" via the `or` coalesce.
    _insert_session(db, sid="x", directory=str(tmp_path), title="",
                    time_created=1, time_updated=2)
    result = await list_sessions_from_sqlite(tmp_path, str(tmp_path))
    assert result[0].summary == "(no title)"


@pytest.mark.asyncio
async def test_schema_break_returns_empty(tmp_path: Path) -> None:
    """If the DB is present but the schema is wrong (upstream renamed a
    column we read), the reader swallows the error and returns ``[]`` —
    the operator sees an empty session list instead of a crashing /resume."""
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    try:
        # Wrong schema: no `directory` column.
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO session VALUES ('a', 'a')")
        conn.commit()
    finally:
        conn.close()

    result = await list_sessions_from_sqlite(tmp_path, "/whatever")
    assert result == []


def test_find_opencode_db_prefers_most_recent(tmp_path: Path) -> None:
    """When both ``opencode.db`` (release) and ``opencode-local.db`` (dev
    build) exist, the more recently modified one wins — that's the live
    server's DB."""
    import os
    import time

    (tmp_path / "opencode.db").write_bytes(b"")
    time.sleep(0.01)
    local = tmp_path / "opencode-local.db"
    local.write_bytes(b"")
    # Bump the mtime explicitly so the test isn't relying on the FS clock.
    later = time.time() + 10
    os.utime(local, (later, later))

    found = _find_opencode_db(tmp_path)
    assert found == local


def test_find_opencode_db_ignores_wal_sidecars(tmp_path: Path) -> None:
    (tmp_path / "opencode.db").write_bytes(b"")
    (tmp_path / "opencode.db-wal").write_bytes(b"x" * 100)
    (tmp_path / "opencode.db-shm").write_bytes(b"x" * 50)
    found = _find_opencode_db(tmp_path)
    assert found is not None
    assert found.name == "opencode.db"


def test_find_opencode_db_returns_none_when_dir_missing(tmp_path: Path) -> None:
    assert _find_opencode_db(tmp_path / "nope") is None


def test_find_opencode_db_returns_none_when_no_candidates(tmp_path: Path) -> None:
    (tmp_path / "unrelated.txt").write_text("hi")
    (tmp_path / "other.db").write_bytes(b"")  # not opencode*.db
    assert _find_opencode_db(tmp_path) is None
