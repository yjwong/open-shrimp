"""Terminal Mini App read/tail endpoints for tasks without output yet."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from open_shrimp.terminal import api
from open_shrimp.terminal.log_source import LogSource


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _no_auth(request: Request) -> int:
        return 1

    monkeypatch.setattr(api, "_authenticate", _no_auth)
    app = Starlette(routes=[
        Route("/api/terminal/tail", api.tail_endpoint, methods=["GET"]),
        Route("/api/terminal/read", api.read_endpoint, methods=["GET"]),
    ])
    return TestClient(app)


def _set_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: set[str],
    sources: dict[str, LogSource],
) -> None:
    monkeypatch.setattr(api, "is_task_active", lambda tid: tid in active)
    monkeypatch.setattr(
        api, "resolve",
        lambda source_type, source_id, **_: sources.get(source_id),
    )


def _done_payload(body: str) -> dict:
    lines = body.splitlines()
    idx = lines.index("event: done")
    return json.loads(lines[idx + 1].removeprefix("data: "))


def test_missing_params_is_400(client: TestClient) -> None:
    resp = client.get("/api/terminal/read?type=task")
    assert resp.status_code == 400
    resp = client.get("/api/terminal/tail?id=abc")
    assert resp.status_code == 400


def test_unknown_task_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_state(monkeypatch, active=set(), sources={})
    assert client.get("/api/terminal/read?type=task&id=gone").status_code == 404
    assert client.get("/api/terminal/tail?type=task&id=gone").status_code == 404


def test_read_pending_task_is_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_state(monkeypatch, active={"quiet"}, sources={})
    resp = client.get("/api/terminal/read?type=task&id=quiet")
    assert resp.status_code == 200
    assert resp.json() == {"id": "quiet", "content": "", "size": 0}


def test_pending_container_build_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_state(monkeypatch, active={"ctx"}, sources={})
    resp = client.get("/api/terminal/read?type=container_build&id=ctx")
    assert resp.status_code == 404


def test_tail_pending_task_that_ends_silently(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"quiet"}
    _set_state(monkeypatch, active=active, sources={})
    monkeypatch.setattr(api, "_PENDING_POLL_INTERVAL", 0.05)

    real_resolve_source = api._resolve_source
    polls = 0

    def _counting_resolve(request: Request) -> LogSource | None:
        nonlocal polls
        polls += 1
        if polls == 3:
            active.discard("quiet")
        return real_resolve_source(request)

    monkeypatch.setattr(api, "_resolve_source", _counting_resolve)

    resp = client.get("/api/terminal/tail?type=task&id=quiet")
    assert resp.status_code == 200
    assert _done_payload(resp.text) == {"completed": True}
    # Initial resolve in the endpoint, then rescans until the task ended.
    assert polls >= 3


def test_tail_streams_output_written_after_connect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    output = tmp_path / "late.output"
    active = {"late"}
    sources: dict[str, LogSource] = {}
    _set_state(monkeypatch, active=active, sources=sources)
    monkeypatch.setattr(api, "_PENDING_POLL_INTERVAL", 0.05)

    def _first_write_then_exit() -> None:
        output.write_text("v0.2.0 sandbox published\n")
        sources["late"] = LogSource(
            path=output, is_active=lambda: "late" in active,
        )
        threading.Timer(0.3, active.discard, args=("late",)).start()

    threading.Timer(0.2, _first_write_then_exit).start()

    resp = client.get("/api/terminal/tail?type=task&id=late")
    assert resp.status_code == 200
    data = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ") and '"text"' in line
    ]
    assert "".join(d["text"] for d in data) == "v0.2.0 sandbox published\n"
    assert _done_payload(resp.text) == {"completed": True}


@pytest.mark.asyncio
async def test_wait_returns_source_once_file_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "late.output"
    source = LogSource(path=output, is_active=lambda: True)
    sources: dict[str, LogSource] = {}
    _set_state(monkeypatch, active={"late"}, sources=sources)
    monkeypatch.setattr(api, "_PENDING_POLL_INTERVAL", 0.05)

    request = Request({
        "type": "http",
        "query_string": b"type=task&id=late",
        "app": Starlette(),
        "headers": [],
    })

    async def _first_write() -> None:
        await asyncio.sleep(0.15)
        output.write_text("published\n")
        sources["late"] = source

    writer = asyncio.create_task(_first_write())
    found = await asyncio.wait_for(
        api._wait_for_task_output(request, asyncio.Event()), timeout=2,
    )
    await writer
    assert found is source


@pytest.mark.asyncio
async def test_wait_stops_on_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_state(monkeypatch, active={"quiet"}, sources={})
    monkeypatch.setattr(api, "_PENDING_POLL_INTERVAL", 30.0)
    request = Request({
        "type": "http",
        "query_string": b"type=task&id=quiet",
        "app": Starlette(),
        "headers": [],
    })
    stop = asyncio.Event()
    waiter = asyncio.create_task(api._wait_for_task_output(request, stop))
    await asyncio.sleep(0.05)
    stop.set()
    assert await asyncio.wait_for(waiter, timeout=1) is None
