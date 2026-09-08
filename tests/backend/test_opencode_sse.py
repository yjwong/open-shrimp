import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from open_shrimp.backend.errors import CLIConnectionError
from open_shrimp.backend.opencode import sse
from open_shrimp.backend.opencode.process import OpenCodeEndpoint, OpenCodeServer
from open_shrimp.backend.opencode.sse import EventBus, EventQueue, EventQueueClosed


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_queue_close_wakes_all_consumers(full: bool, failed: bool) -> None:
    queue = EventQueue("session", maxsize=1)
    if full:
        queue.put_nowait({"type": "buffered"})
    error = CLIConnectionError("endpoint lost") if failed else None
    if full:
        queue.close(error)
    consumers = [asyncio.create_task(queue.get()) for _ in range(3)]
    await asyncio.sleep(0)
    queue.close(error)
    queue.put_nowait({"type": "ignored"})
    results = await asyncio.wait_for(
        asyncio.gather(*consumers, return_exceptions=True), 1,
    )
    for result in results:
        if failed:
            assert result is error
        else:
            assert isinstance(result, EventQueueClosed)
    with pytest.raises(CLIConnectionError if failed else EventQueueClosed):
        await asyncio.wait_for(queue.get(), 1)


@pytest.mark.asyncio
async def test_cancel_start_closes_reader_and_owned_http(monkeypatch) -> None:
    bus = EventBus(OpenCodeEndpoint("http://localhost:1234", "Basic secret"))
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def read() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(bus, "_read_stream", read)
    queue = bus.subscribe("session")
    start = asyncio.create_task(bus.start())
    await asyncio.wait_for(entered.wait(), 1)
    reader = bus._reader_task
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert exited.is_set()
    assert reader.done()
    assert bus._http.is_closed
    assert not bus.is_alive()
    with pytest.raises(EventQueueClosed):
        await queue.get()
    await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["host", "sandbox", "replacement", "removed"])
@pytest.mark.parametrize("connected", [False, True])
async def test_endpoint_loss_terminates_hung_stream(monkeypatch, failure, connected) -> None:
    monkeypatch.setattr(sse, "_HEALTH_INTERVAL", 0.001)
    proc = Mock()
    proc.returncode = None
    proc.poll.return_value = None
    owner = SimpleNamespace(_served_proc=proc)
    endpoint = (
        OpenCodeServer(proc, "http://localhost:1234", "secret", "opencode")
        if failure == "host"
        else OpenCodeEndpoint("http://localhost:1234", "Basic secret", owner)
    )
    bus = EventBus(endpoint, queue_size=1)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def read() -> None:
        if connected:
            bus._dispatch({"type": "server.connected"})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(bus, "_read_stream", read)
    queue = bus.subscribe("session")
    queue.put_nowait({"type": "buffered"})
    start = asyncio.create_task(bus.start())
    await asyncio.wait_for(entered.wait(), 1)
    if connected:
        await start
    if failure == "host":
        proc.returncode = 1
    elif failure == "sandbox":
        proc.poll.return_value = 1
    elif failure == "replacement":
        owner._served_proc = Mock()
        owner._served_proc.poll.return_value = None
    else:
        owner._served_proc = None
    assert not bus.is_alive()
    if not connected:
        with pytest.raises(CLIConnectionError, match="exited or.*replaced"):
            await asyncio.wait_for(start, 1)
    reader = bus._reader_task
    await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 1)
    assert exited.is_set()
    assert bus._http.is_closed
    for closed in (queue, bus._broadcast, bus.subscribe("late")):
        with pytest.raises(CLIConnectionError):
            await asyncio.wait_for(closed.get(), 1)
    await bus.stop()
    assert reader.done()
    assert not bus.is_alive()
    with pytest.raises(CLIConnectionError):
        await bus.start()


@pytest.mark.asyncio
async def test_live_endpoint_retries_and_keeps_borrowed_http(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sse, "_BACKOFF_INITIAL", 0.001)
    proc = Mock()
    proc.poll.return_value = None
    endpoint = OpenCodeEndpoint(
        "http://localhost:1234", "Basic secret", SimpleNamespace(_served_proc=proc),
    )
    requests = []
    stream_closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"server.connected"}\n\n'
            yield b'data: {"type":"message.updated","properties":{"sessionID":"s"}}\n\n'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            stream_closed.set()

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("transient Basic secret", request=request)
        if len(requests) == 2:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(
        base_url=endpoint.base_url, transport=httpx.MockTransport(handle),
    ) as http:
        bus = EventBus(endpoint, directory="/workspace", http_client=http)
        queue = bus.subscribe("s")
        await asyncio.wait_for(bus.start(), 1)
        assert bus.is_alive()
        assert (await asyncio.wait_for(queue.get(), 1))["type"] == "message.updated"
        assert len(requests) == 3
        assert all(r.url.params["directory"] == "/workspace" for r in requests)
        await bus.stop()
        assert stream_closed.is_set()
        assert not http.is_closed
    assert "url=http://localhost:1234" in caplog.text
    assert "directory='/workspace' subscribers=1" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_unowned_endpoint_health_and_completed_reader() -> None:
    async with httpx.AsyncClient() as http:
        bus = EventBus(OpenCodeEndpoint("http://localhost:1234", ""), http_client=http)
        assert bus.is_alive()
        bus._reader_task = asyncio.create_task(asyncio.sleep(0))
        await bus._reader_task
        assert not bus.is_alive()
        await bus.stop()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_start_timeout_cleans_up(monkeypatch) -> None:
    monkeypatch.setattr(sse, "_CONNECT_TIMEOUT", 0.01)
    bus = EventBus(OpenCodeEndpoint("http://localhost:1234", ""))

    async def read() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(bus, "_read_stream", read)
    with pytest.raises(CLIConnectionError, match="did not emit"):
        await bus.start()
    assert bus._http.is_closed
    assert not bus.is_alive()


@pytest.mark.asyncio
async def test_endpoint_death_during_reconnect_backoff(monkeypatch) -> None:
    monkeypatch.setattr(sse, "_HEALTH_INTERVAL", 0.001)
    monkeypatch.setattr(sse, "_BACKOFF_INITIAL", 30)
    proc = Mock(returncode=None)
    server = OpenCodeServer(proc, "http://localhost:1234", "secret", "opencode")
    bus = EventBus(server)
    attempts = 0

    async def read() -> None:
        nonlocal attempts
        attempts += 1
        proc.returncode = 1
        raise httpx.ConnectError("connection lost")

    monkeypatch.setattr(bus, "_read_stream", read)
    queue = bus.subscribe("session")
    with pytest.raises(CLIConnectionError, match="exited"):
        await asyncio.wait_for(bus.start(), 1)
    assert attempts == 1
    assert bus._reader_task.done()
    assert bus._http.is_closed
    with pytest.raises(CLIConnectionError):
        await queue.get()
    await bus.stop()
