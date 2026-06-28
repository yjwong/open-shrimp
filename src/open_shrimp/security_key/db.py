"""SQLite helpers for security-key forwarding metadata.

The relay must never persist HID or CTAP payload bytes. These helpers only
record session metadata and coarse audit events.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite

from open_shrimp.db import ChatScope


def _thread_id_to_db(thread_id: int | None) -> int:
    return thread_id if thread_id is not None else 0


def _session_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "chat_id": row[1],
        "thread_id": None if row[2] == 0 else row[2],
        "context_name": row[3],
        "sandbox_id": row[4],
        "device_id": row[5],
        "status": row[6],
        "created_at": row[7],
        "expires_at": row[8],
        "approved_at": row[9],
        "ended_at": row[10],
        "end_reason": row[11],
        "requested_device_id": row[12],
        "claimed_device_id": row[13],
        "push_sent_at": row[14],
        "push_status": row[15],
    }


async def create_security_key_session_record(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    scope: ChatScope,
    context_name: str,
    sandbox_id: str | None,
    expires_at: int,
) -> None:
    now = int(time.time())
    await db.execute(
        """
        INSERT INTO security_key_sessions (
            id, chat_id, message_thread_id, context_name, sandbox_id,
            status, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            scope.chat_id,
            _thread_id_to_db(scope.thread_id),
            context_name,
            sandbox_id,
            "created",
            now,
            expires_at,
        ),
    )
    await db.commit()


async def update_security_key_session_status(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    status: str,
    end_reason: str | None = None,
) -> None:
    now = int(time.time())
    if end_reason is None:
        await db.execute(
            "UPDATE security_key_sessions SET status = ? WHERE id = ?",
            (status, session_id),
        )
    else:
        await db.execute(
            """
            UPDATE security_key_sessions
            SET status = ?, ended_at = ?, end_reason = ?
            WHERE id = ?
            """,
            (status, now, end_reason, session_id),
        )
    await db.commit()


async def mark_security_key_session_approved(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    device_id: str | None,
) -> None:
    now = int(time.time())
    await db.execute(
        """
        UPDATE security_key_sessions
        SET status = ?, approved_at = ?, device_id = ?
        WHERE id = ?
        """,
        ("approved", now, device_id, session_id),
    )
    await db.commit()


async def get_security_key_session_record(
    db: aiosqlite.Connection,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    cursor = await db.execute(
        """
        SELECT id, chat_id, message_thread_id, context_name, sandbox_id,
               device_id, status, created_at, expires_at, approved_at,
               ended_at, end_reason, requested_device_id, claimed_device_id,
               push_sent_at, push_status
        FROM security_key_sessions
        WHERE id = ?
        """,
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _session_row_to_dict(row)


async def list_pending_android_security_key_sessions(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    now = int(time.time())
    cursor = await db.execute(
        """
        SELECT id, chat_id, message_thread_id, context_name, sandbox_id,
               device_id, status, created_at, expires_at, approved_at,
               ended_at, end_reason, requested_device_id, claimed_device_id,
               push_sent_at, push_status
        FROM security_key_sessions
        WHERE ended_at IS NULL
          AND expires_at > ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (now,),
    )
    rows = await cursor.fetchall()
    return [_session_row_to_dict(row) for row in rows]


async def mark_security_key_session_claimed(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    device_id: str,
) -> None:
    await db.execute(
        "UPDATE security_key_sessions SET claimed_device_id = ? WHERE id = ?",
        (device_id, session_id),
    )
    await db.commit()


async def audit_security_key_event(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    event: str,
    role: str | None = None,
    reason: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO security_key_audit_events (
            session_id, event, role, reason, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, event, role, reason, int(time.time())),
    )
    await db.commit()
