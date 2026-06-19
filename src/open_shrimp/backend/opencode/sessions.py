"""Session listing for OpenCode.

Two paths:

* :func:`list_sessions` — HTTP via ``GET /session`` against a running
  ``opencode serve`` (host-local default, or sandbox-served if the caller
  passes ``base_url`` / ``auth_header``).
* :func:`list_sessions_from_sqlite` — read OpenCode's on-disk SQLite
  database directly.  Used for the sandboxed ``/resume`` listing so the bot
  doesn't have to boot the sandbox just to enumerate sessions.

OpenCode stores its session corpus in
``$XDG_DATA_HOME/opencode/opencode[-channel].db`` (Drizzle ORM, WAL mode).
The schema lives in the upstream repo at
``packages/opencode/src/session/session.sql.ts`` — load-bearing columns we
read (``id``, ``title``, ``directory``, ``time_created``, ``time_updated``,
``time_archived``) are from the original schema baseline and are the same
columns the HTTP listing reads, so the on-disk path returns the same rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from open_shrimp.backend.errors import ProcessError
from open_shrimp.backend.opencode._http import get_json, get_json_from_server
from open_shrimp.backend.sessions import SessionInfo

logger = logging.getLogger(__name__)


async def list_sessions(
    directory: str | Path,
    *,
    limit: int = 500,
    base_url: str | None = None,
    auth_header: str | None = None,
) -> list[SessionInfo]:
    """List sessions whose directory matches *directory*, newest first.

    ``directory`` is canonicalised before the request — OpenCode does
    an exact string match server-side. OpenCode silently ignores the
    ``offset`` query parameter, so callers paginate client-side.

    Returns backend-neutral ``backend.SessionInfo`` rows.  OpenCode has no
    equivalent for the vestigial fields (``custom_title`` / ``first_prompt``
    / ``git_branch`` / ``file_size`` / ``cwd`` / ``tag``), so they keep their
    defaults; the resume-detail renderer reads them uniformly across sources.
    """
    # Empty directory returns the global session list across every
    # project — a privacy leak we never want, so reject it explicitly.
    raw = str(directory)
    if not raw.strip():
        raise ValueError("list_sessions requires a non-empty directory")
    canonical = str(Path(raw).resolve())

    params = {"directory": canonical, "limit": limit}
    if base_url is not None or auth_header is not None:
        if base_url is None or auth_header is None:
            raise ValueError("base_url and auth_header must be provided together")
        rows = await get_json_from_server(
            base_url, auth_header, "/session", params=params,
        )
    else:
        rows = await get_json("/session", params=params)
    if not isinstance(rows, list):
        raise ProcessError(f"GET /session returned non-list: {rows!r}")

    out: list[SessionInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        if not sid:
            continue
        time_block = row.get("time") or {}
        out.append(
            SessionInfo(
                session_id=str(sid),
                summary=row.get("title") or "(no title)",
                last_modified=int(time_block.get("updated") or 0),
                created_at=(
                    int(time_block["created"])
                    if isinstance(time_block.get("created"), (int, float))
                    else None
                ),
            )
        )
    return out


def _find_opencode_db(db_dir: Path) -> Path | None:
    """Locate the OpenCode SQLite database inside ``db_dir``.

    OpenCode writes ``opencode.db`` for release channels (``latest``/``beta``/
    ``prod``) and ``opencode-<channel>.db`` for everything else (e.g. a
    ``local`` dev build).  We don't know which channel ran inside the sandbox,
    so we pick the most recently modified ``opencode*.db`` file in the
    directory — that's the one the live ``opencode serve`` was writing to.
    Returns ``None`` if no candidate exists (a fresh sandbox that has never
    run ``opencode serve``).
    """
    if not db_dir.is_dir():
        return None
    candidates = [
        p for p in db_dir.glob("opencode*.db")
        if p.is_file() and not p.name.endswith((".db-wal", ".db-shm"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


async def list_sessions_from_sqlite(
    db_dir: Path,
    directory: str | Path,
    *,
    limit: int = 500,
) -> list[SessionInfo]:
    """List sessions by reading OpenCode's SQLite database directly.

    *db_dir* is the directory that contains ``opencode[-channel].db``
    (typically the sandbox-mapped ``host_dir`` from the OpenCode runtime's
    :class:`HomeMount`).  *directory* is the working directory to filter on;
    canonicalised before the query (the column is populated with the resolved
    path on the OpenCode side, so the match must be exact).

    Opens the database read-only with ``mode=ro`` + ``PRAGMA query_only`` so
    a running ``opencode serve`` writing through WAL is unaffected.  On any
    error (missing DB, schema mismatch, locked DB) returns ``[]`` and logs —
    the bot's ``/resume`` UX treats "no sessions found" as the safe degrade.

    Returns the same ``SessionInfo`` shape as the HTTP path: ``session_id``
    from ``id``, ``summary`` from ``title``, ``last_modified`` from
    ``time_updated``, ``created_at`` from ``time_created``.  Skips archived
    sessions (``time_archived IS NULL``) to match the HTTP default.
    """
    import aiosqlite

    raw = str(directory)
    if not raw.strip():
        raise ValueError("list_sessions_from_sqlite requires a non-empty directory")
    canonical = str(Path(raw).resolve())

    db_path = _find_opencode_db(db_dir)
    if db_path is None:
        return []

    try:
        uri = f"file:{db_path}?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as conn:
            await conn.execute("PRAGMA query_only = 1")
            async with conn.execute(
                """
                SELECT id, title, time_updated, time_created
                FROM session
                WHERE directory = ?
                  AND time_archived IS NULL
                ORDER BY time_updated DESC, id DESC
                LIMIT ?
                """,
                (canonical, limit),
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.warning(
            "Reading opencode session DB at %s failed; "
            "returning empty session list",
            db_path,
            exc_info=True,
        )
        return []

    out: list[SessionInfo] = []
    for sid, title, time_updated, time_created in rows:
        if not sid:
            continue
        out.append(
            SessionInfo(
                session_id=str(sid),
                summary=title or "(no title)",
                last_modified=int(time_updated or 0),
                created_at=(
                    int(time_created) if time_created is not None else None
                ),
            )
        )
    return out


__all__ = ["list_sessions", "list_sessions_from_sqlite"]
