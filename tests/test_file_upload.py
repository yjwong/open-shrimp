"""Tests for the upload Mini App API: key exchange, resumable chunks, and the
hand-off to the agent as staged attachments."""

from __future__ import annotations

import time
from collections import namedtuple
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

from open_shrimp import dispatch_registry, hooks
from open_shrimp.agent import FileAttachment, save_attachments
from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.file_upload import api as upload_api
from open_shrimp.file_upload.api import create_upload_routes, open_session
from open_shrimp.supervisor import SupervisorDispatchRefused

from tests.preview.test_api import ALLOWED_USER_ID, BOT_TOKEN, _auth_header

CHAT_ID = 42


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(hooks, "ATTACHMENT_TEMP_DIR", uploads)
    monkeypatch.setattr(upload_api, "ATTACHMENT_TEMP_DIR", uploads)
    upload_api._sessions.clear()
    yield
    upload_api._sessions.clear()


def _client(tmp_path) -> AsyncClient:
    app = Starlette(routes=create_upload_routes())
    app.state.config = Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory=str(tmp_path), description="", allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    )


async def _start(client: AsyncClient, sid: str) -> dict[str, str]:
    resp = await client.post(f"/api/upload/{sid}/start", headers=_auth_header())
    assert resp.status_code == 200
    return {"x-upload-key": resp.json()["key"]}


async def _create(client, sid, key, name="data.csv", size=10) -> str:
    resp = await client.post(
        f"/api/upload/{sid}/files", json={"name": name, "size": size}, headers=key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["file_id"]


async def _put(client, sid, key, fid, offset, data):
    return await client.put(
        f"/api/upload/{sid}/files/{fid}?offset={offset}", content=data, headers=key,
    )


# --- key exchange -----------------------------------------------------------


@pytest.mark.asyncio
async def test_start_requires_telegram_auth(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        resp = await client.post(f"/api/upload/{sid}/start")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_keyed_calls_reject_a_wrong_key(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        await _start(client, sid)
        resp = await client.post(
            f"/api/upload/{sid}/files", json={"name": "a", "size": 1},
            headers={"x-upload-key": "guess"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_start_returns_caption(tmp_path) -> None:
    sid = open_session(CHAT_ID, None, caption="summarise this")
    async with _client(tmp_path) as client:
        resp = await client.post(f"/api/upload/{sid}/start", headers=_auth_header())
    assert resp.json()["caption"] == "summarise this"
    assert resp.json()["chunk_bytes"] == upload_api.CHUNK_BYTES


@pytest.mark.asyncio
async def test_reopening_drops_half_sent_files(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key)
        path = upload_api._sessions[sid].files[fid].path
        assert path.exists()
        new_key = await _start(client, sid)
        assert not path.exists()
        # The old key no longer works.
        resp = await client.get(f"/api/upload/{sid}/files/{fid}", headers=key)
        assert resp.status_code == 404
        assert new_key != key


# --- chunks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunks_append_and_resume_from_server_offset(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, size=10)

        resp = await _put(client, sid, key, fid, 0, b"01234")
        assert resp.json() == {"received": 5}

        # A retried chunk whose first attempt landed is told where to resume.
        resp = await _put(client, sid, key, fid, 0, b"01234")
        assert resp.status_code == 409
        assert resp.json() == {"received": 5}

        resp = await _put(client, sid, key, fid, 5, b"56789")
        assert resp.json() == {"received": 10}

        resp = await client.get(f"/api/upload/{sid}/files/{fid}", headers=key)
        assert resp.json() == {"received": 10, "size": 10}

    path = upload_api._sessions[sid].files[fid].path
    assert path.read_bytes() == b"0123456789"
    assert path.parent == hooks.ATTACHMENT_TEMP_DIR / str(CHAT_ID)
    assert path.name.startswith("openshrimp_data.csv_")
    assert path.suffix == ".csv"


@pytest.mark.asyncio
async def test_chunk_past_declared_size_is_rejected(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, size=3)
        resp = await _put(client, sid, key, fid, 0, b"toolong")
    assert resp.status_code == 400
    assert upload_api._sessions[sid].files[fid].received == 0


@pytest.mark.asyncio
async def test_oversized_chunk_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(upload_api, "CHUNK_BYTES", 4)
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, size=100)
        resp = await _put(client, sid, key, fid, 0, b"12345")
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_refuses_file_that_would_fill_the_disk(tmp_path, monkeypatch) -> None:
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        upload_api.shutil, "disk_usage", lambda _: Usage(0, 0, 600 * 2**20),
    )
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        resp = await client.post(
            f"/api/upload/{sid}/files",
            json={"name": "big.iso", "size": 200 * 2**20}, headers=key,
        )
    assert resp.status_code == 507
    assert "Not enough space" in resp.json()["error"]


# --- send -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dispatches_staged_attachments(tmp_path, monkeypatch) -> None:
    fake = AsyncMock()
    monkeypatch.setattr(dispatch_registry, "dispatch", fake)
    sid = open_session(CHAT_ID, 7)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, name="clip.mp4", size=3)
        await _put(client, sid, key, fid, 0, b"abc")
        resp = await client.post(
            f"/api/upload/{sid}/send", json={"message": "trim it"}, headers=key,
        )
        assert resp.status_code == 200
        # Single use.
        resp = await client.post(
            f"/api/upload/{sid}/send", json={"message": ""}, headers=key,
        )
        assert resp.status_code == 404

    fake.assert_awaited_once()
    args, kwargs = fake.await_args
    assert args == ("trim it", CHAT_ID, 7)
    (att,) = kwargs["attachments"]
    assert att.filename == "clip.mp4"
    assert att.mime_type == "video/mp4"
    assert att.staged.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_send_without_message_uses_default_prompt(tmp_path, monkeypatch) -> None:
    fake = AsyncMock()
    monkeypatch.setattr(dispatch_registry, "dispatch", fake)
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        for name in ("a.txt", "b.txt"):
            fid = await _create(client, sid, key, name=name, size=1)
            await _put(client, sid, key, fid, 0, b"x")
        await client.post(f"/api/upload/{sid}/send", json={}, headers=key)
    assert fake.await_args.args[0] == "What's in these files?"


@pytest.mark.asyncio
async def test_send_waits_for_every_file(tmp_path, monkeypatch) -> None:
    fake = AsyncMock()
    monkeypatch.setattr(dispatch_registry, "dispatch", fake)
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, size=4)
        await _put(client, sid, key, fid, 0, b"ab")
        resp = await client.post(f"/api/upload/{sid}/send", json={}, headers=key)
    assert resp.status_code == 409
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_dispatch_keeps_files_for_a_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch_registry, "dispatch",
        AsyncMock(side_effect=SupervisorDispatchRefused("supervisor says no")),
    )
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key, size=1)
        await _put(client, sid, key, fid, 0, b"x")
        resp = await client.post(f"/api/upload/{sid}/send", json={}, headers=key)
    assert resp.status_code == 409
    assert resp.json()["error"] == "supervisor says no"
    assert upload_api._sessions[sid].files[fid].path.exists()


# --- cleanup ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_deletes_files(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key)
        path = upload_api._sessions[sid].files[fid].path
        resp = await client.post(f"/api/upload/{sid}/cancel", headers=key)
    assert resp.status_code == 200
    assert not path.exists()
    assert sid not in upload_api._sessions


@pytest.mark.asyncio
async def test_idle_sessions_are_purged_with_their_files(tmp_path) -> None:
    sid = open_session(CHAT_ID, None)
    async with _client(tmp_path) as client:
        key = await _start(client, sid)
        fid = await _create(client, sid, key)
    session = upload_api._sessions[sid]
    path = session.files[fid].path
    session.touched = time.monotonic() - upload_api._SESSION_IDLE - 1

    open_session(CHAT_ID, None)

    assert sid not in upload_api._sessions
    assert not path.exists()


def test_save_attachments_hands_staged_files_on_unchanged(tmp_path) -> None:
    staged = tmp_path / "big.bin"
    staged.write_bytes(b"x")
    att = FileAttachment(data=b"", mime_type="application/octet-stream", staged=staged)
    assert save_attachments([att], CHAT_ID) == [staged]
