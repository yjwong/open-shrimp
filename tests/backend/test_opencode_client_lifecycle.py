import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from open_shrimp.backend.errors import CLIConnectionError
from open_shrimp.backend.opencode import client as oc
from open_shrimp.backend.opencode import sse
from open_shrimp.backend.opencode.process import OpenCodeEndpoint
from open_shrimp.backend.opencode.sse import EventBus, EventQueueClosed
from open_shrimp.backend.protocol import BackendOptions
from open_shrimp.backend.types import (
    AssistantMessage,
    Message,
    PermissionResultAllow,
    ResultMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
)


@pytest_asyncio.fixture(autouse=True)
async def isolated_buses(monkeypatch) -> AsyncIterator[list[EventBus]]:
    monkeypatch.setattr(oc, "_BUS_REGISTRY", {})
    monkeypatch.setattr(oc, "_BUS_LOCK", None)
    buses = []

    def make_bus(*args: Any, **kwargs: Any) -> EventBus:
        bus = EventBus(*args, **kwargs)
        buses.append(bus)
        return bus

    async def read(bus: EventBus) -> None:
        bus._dispatch({"type": "server.connected"})
        await asyncio.Event().wait()

    monkeypatch.setattr(oc, "EventBus", make_bus)
    monkeypatch.setattr(EventBus, "_read_stream", read)
    try:
        yield buses
    finally:
        await oc._shutdown_buses()
        # Failed acquisition never registers its bus.
        for bus in buses:
            await bus.stop()


@pytest.fixture
def endpoint() -> OpenCodeEndpoint:
    return OpenCodeEndpoint("http://localhost:1234", "Basic secret")


@pytest_asyncio.fixture
async def make_client(monkeypatch, endpoint, isolated_buses) -> AsyncIterator[Callable]:
    clients = []
    monkeypatch.setattr(oc.OpenCodeClient, "_load_context_window", AsyncMock())
    monkeypatch.setattr(oc.OpenCodeClient, "_register_mcp_servers", AsyncMock())

    def make(*, host: bool = False) -> oc.OpenCodeClient:
        client = oc.OpenCodeClient(BackendOptions(
            cwd="/workspace", model="openai/test-model",
            extra={} if host else {"endpoint": endpoint},
        ))
        monkeypatch.setattr(
            client, "_create_session", AsyncMock(return_value=f"session-{len(clients)}"),
        )
        clients.append(client)
        return client

    try:
        yield make
    finally:
        for client in clients:
            await client.disconnect()


def assert_disconnected(client: oc.OpenCodeClient) -> None:
    assert client._server is None
    assert client._bus is None
    assert client._http is None
    assert client._events is None
    assert client._bridge is None
    assert client.session_id is None
    assert not client.is_alive()


@pytest.mark.asyncio
async def test_acquire_release_refcounts_and_closes_last_reader(endpoint) -> None:
    bus = await oc._acquire_bus(endpoint, None)
    reader = bus._reader_task
    queue = bus.subscribe("session")
    assert await oc._acquire_bus(endpoint, "") is bus
    key = (endpoint.base_url, endpoint.auth_header, "")
    assert oc._BUS_REGISTRY == {key: (bus, 2)}

    await oc._release_bus(bus)
    assert oc._BUS_REGISTRY == {key: (bus, 1)}
    assert bus.is_alive()
    assert not bus._http.is_closed
    assert not reader.done()

    await oc._release_bus(bus)
    assert oc._BUS_REGISTRY == {}
    assert reader.done()
    assert bus._reader_task is None
    assert bus._http.is_closed
    with pytest.raises(EventQueueClosed):
        await asyncio.wait_for(queue.get(), 1)
    await oc._release_bus(bus)
    assert oc._BUS_REGISTRY == {}


@pytest.mark.asyncio
async def test_shared_clients_disconnect_independently(make_client, endpoint) -> None:
    first, second = make_client(), make_client()
    await first.connect()
    await second.connect()
    bus = first._bus
    assert second._bus is bus
    reader = bus._reader_task
    first_http, second_http = first._http, second._http
    first_queue, second_queue = first._events, second._events
    key = (endpoint.base_url, endpoint.auth_header, "/workspace")
    assert oc._BUS_REGISTRY == {key: (bus, 2)}

    await first.disconnect()
    assert_disconnected(first)
    assert first_http.is_closed
    assert second.is_alive()
    assert not second_http.is_closed
    assert oc._BUS_REGISTRY == {key: (bus, 1)}
    with pytest.raises(EventQueueClosed):
        await asyncio.wait_for(first_queue.get(), 1)
    event = {"type": "session.status", "properties": {"sessionID": second.session_id}}
    bus._dispatch(event)
    assert await asyncio.wait_for(second_queue.get(), 1) == event

    await first.disconnect()
    assert oc._BUS_REGISTRY == {key: (bus, 1)}
    await second.disconnect()
    await second.disconnect()
    assert_disconnected(second)
    assert second_http.is_closed
    assert bus._http.is_closed
    assert reader.done()
    assert oc._BUS_REGISTRY == {}
    with pytest.raises(EventQueueClosed):
        await asyncio.wait_for(second_queue.get(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_disconnect_finishes_while_bus_lock_is_held(
    make_client, endpoint, shared: bool,
) -> None:
    client = make_client()
    await client.connect()
    bus, http, queue = client._bus, client._http, client._events
    reader = bus._reader_task
    if shared:
        other = make_client()
        await other.connect()
        assert other._bus is bus

    async with oc._BUS_LOCK:
        await asyncio.wait_for(client.disconnect(), 1)
        assert oc._BUS_LOCK.locked()
        assert_disconnected(client)
        assert http.is_closed
        with pytest.raises(EventQueueClosed):
            await asyncio.wait_for(queue.get(), 1)
        if shared:
            key = (endpoint.base_url, endpoint.auth_header, "/workspace")
            assert oc._BUS_REGISTRY == {key: (bus, 1)}
            assert other.is_alive()
            assert not bus._http.is_closed
            assert not reader.done()
        else:
            assert oc._BUS_REGISTRY == {}
            assert bus._http.is_closed
            assert reader.done()
            assert bus._reader_task is None


@pytest.mark.asyncio
async def test_cancel_final_disconnect_waits_for_bus_cleanup(monkeypatch, make_client) -> None:
    client = make_client()
    await client.connect()
    bus, http, queue = client._bus, client._http, client._events
    reader = bus._reader_task
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup = bus._cleanup

    async def gated_cleanup() -> None:
        cleanup_started.set()
        try:
            await finish_cleanup.wait()
            await cleanup()
            cleaned.set()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            raise

    monkeypatch.setattr(bus, "_cleanup", gated_cleanup)
    disconnect = asyncio.create_task(client.disconnect())
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert oc._BUS_REGISTRY == {}
        assert not bus._http.is_closed
        assert not http.is_closed
        disconnect.cancel()
        # Let cancellation reach the shield and any unshielded reader await.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not disconnect.done()
        assert not cleanup_cancelled.is_set()
        assert not cleaned.is_set()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(disconnect, 1)
        assert cleaned.is_set()
        assert not cleanup_cancelled.is_set()
        assert_disconnected(client)
        assert oc._BUS_REGISTRY == {}
        assert reader.done()
        assert bus._reader_task is None
        assert bus._http.is_closed
        assert http.is_closed
        assert bus._subscribers == {}
        for closed in (queue, bus._broadcast):
            with pytest.raises(EventQueueClosed):
                await asyncio.wait_for(closed.get(), 1)
    finally:
        finish_cleanup.set()
        await asyncio.wait_for(asyncio.gather(disconnect, return_exceptions=True), 1)


@pytest.mark.asyncio
async def test_disconnect_before_connect_is_idempotent(make_client) -> None:
    client = make_client()
    await client.disconnect()
    await client.disconnect()
    assert_disconnected(client)
    assert oc._BUS_REGISTRY == {}
    assert oc._BUS_LOCK is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("stage", ["server", "bus", "session"])
async def test_connect_failure_cleans_up(
    monkeypatch, make_client, isolated_buses, cancelled: bool, stage: str,
) -> None:
    client = make_client(host=stage == "server")
    entered = asyncio.Event()
    error = CLIConnectionError(f"{stage} failed")
    http_clients = []

    async def fail(*args: Any, **kwargs: Any) -> None:
        if client._http is not None:
            http_clients.append(client._http)
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise error

    if stage == "server":
        monkeypatch.setattr(oc.OpenCodeServer, "get_or_start", fail)
    elif stage == "bus":
        if cancelled:
            monkeypatch.setattr(EventBus, "_read_stream", fail)
        else:
            # Exercise real start() timeout cleanup before registry insertion.
            async def read(bus: EventBus) -> None:
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(EventBus, "_read_stream", read)
            monkeypatch.setattr(sse, "_CONNECT_TIMEOUT", 0.01)
    else:
        monkeypatch.setattr(client, "_create_session", fail)

    task = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancelled:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancelled else CLIConnectionError):
            await asyncio.wait_for(task, 1)
        assert_disconnected(client)
        assert oc._BUS_REGISTRY == {}
        assert all(http.is_closed for http in http_clients)
        assert len(http_clients) == (1 if stage == "session" else 0)
        assert len(isolated_buses) == (0 if stage == "server" else 1)
        for bus in isolated_buses:
            assert not bus.is_alive()
            assert bus._http.is_closed
            assert bus._reader_task is None or bus._reader_task.done()
        await client.disconnect()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_connect_preserves_existing_bus(
    monkeypatch, make_client, endpoint, cancelled: bool,
) -> None:
    first, second = make_client(), make_client()
    await first.connect()
    bus = first._bus
    entered = asyncio.Event()
    http_clients = []

    async def fail() -> str:
        http_clients.append(second._http)
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise CLIConnectionError("session creation failed")

    monkeypatch.setattr(second, "_create_session", fail)
    task = asyncio.create_task(second.connect())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancelled:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancelled else CLIConnectionError):
            await asyncio.wait_for(task, 1)
        assert_disconnected(second)
        assert http_clients[0].is_closed
        key = (endpoint.base_url, endpoint.auth_header, "/workspace")
        assert oc._BUS_REGISTRY == {key: (bus, 1)}
        assert first.is_alive()
        assert not bus._http.is_closed
        event = {"type": "session.status", "properties": {"sessionID": first.session_id}}
        bus._dispatch(event)
        assert await asyncio.wait_for(first._events.get(), 1) == event
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dead_bus_replacement_ignores_old_releases(endpoint) -> None:
    old = await oc._acquire_bus(endpoint, "/workspace")
    assert await oc._acquire_bus(endpoint, "/workspace") is old
    reader = old._reader_task
    reader.cancel()
    await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 1)
    assert not old.is_alive()

    new = await oc._acquire_bus(endpoint, "/workspace")
    assert new is not old
    assert old._reader_task is None
    assert old._http.is_closed
    assert await oc._acquire_bus(endpoint, "/workspace") is new
    key = (endpoint.base_url, endpoint.auth_header, "/workspace")
    await oc._release_bus(old)
    await oc._release_bus(old)
    assert oc._BUS_REGISTRY == {key: (new, 2)}
    assert new.is_alive()
    assert not new._http.is_closed
    await oc._release_bus(new)
    assert oc._BUS_REGISTRY == {key: (new, 1)}
    await oc._release_bus(new)
    assert new._http.is_closed
    assert oc._BUS_REGISTRY == {}


@pytest.mark.asyncio
async def test_dead_bus_replacement_fails_old_queue_before_watcher(monkeypatch) -> None:
    monkeypatch.setattr(sse, "_HEALTH_INTERVAL", 3600)
    proc = Mock()
    proc.poll.return_value = None
    owner = SimpleNamespace(_served_proc=proc)
    endpoint = OpenCodeEndpoint("http://localhost:1234", "Basic secret", owner)
    old = await oc._acquire_bus(endpoint, "/workspace")
    queue = old.subscribe("old-session")
    reader = old._reader_task
    assert proc.poll.called
    assert old._terminal_error is None

    replacement = Mock()
    replacement.poll.return_value = None
    owner._served_proc = replacement
    assert not old.is_alive()
    assert not reader.done()
    assert not old._stop.is_set()
    assert old._terminal_error is None
    # No yield between endpoint replacement and acquisition; stop supplies the error.
    new = await oc._acquire_bus(endpoint, "/workspace")
    assert new is not old
    with pytest.raises(CLIConnectionError, match="exited or.*replaced") as caught:
        await asyncio.wait_for(queue.get(), 1)
    assert caught.value is old._terminal_error
    assert reader.done()
    assert old._reader_task is None
    assert old._http.is_closed
    await oc._release_bus(old)
    key = (endpoint.base_url, endpoint.auth_header, "/workspace")
    assert oc._BUS_REGISTRY == {key: (new, 1)}
    assert new.is_alive()
    assert not new._http.is_closed
    await oc._release_bus(new)
    assert oc._BUS_REGISTRY == {}
    assert new._http.is_closed


@pytest.mark.asyncio
async def test_shutdown_buses_closes_every_bus_regardless_of_refcount(endpoint) -> None:
    await oc._shutdown_buses()
    buses = [await oc._acquire_bus(endpoint, directory) for directory in ("/a", "/b")]
    assert await oc._acquire_bus(endpoint, "/a") is buses[0]
    readers = [bus._reader_task for bus in buses]
    queues = [bus.subscribe("session") for bus in buses]
    await oc._shutdown_buses()
    assert oc._BUS_REGISTRY == {}
    for bus, reader, queue in zip(buses, readers, queues):
        assert reader.done()
        assert bus._reader_task is None
        assert bus._http.is_closed
        assert not bus.is_alive()
        with pytest.raises(EventQueueClosed):
            await asyncio.wait_for(queue.get(), 1)
        await oc._release_bus(bus)
    await oc._shutdown_buses()
    assert oc._BUS_REGISTRY == {}


@pytest.mark.asyncio
async def test_receive_response_propagates_queue_connection_error(make_client) -> None:
    client = make_client()
    await client.connect()
    error = CLIConnectionError("endpoint lost")
    client._events.close(error)
    response = client.receive_response()
    try:
        with pytest.raises(CLIConnectionError) as caught:
            await asyncio.wait_for(anext(response), 1)
        assert caught.value is error
    finally:
        await response.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_producer", ["parent", "child"])
async def test_receive_response_awaits_sibling_cancellation(
    monkeypatch, make_client, failed_producer: str,
) -> None:
    client = make_client()
    await client.connect()
    child_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    error = CLIConnectionError(f"{failed_producer} lost endpoint")
    producer_tasks = []
    started = TaskStartedMessage(
        subtype="task_started", data={}, task_id="child", tool_use_id="call",
    )

    async def wait_for_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            cleaned.set()

    async def parent(*args: Any) -> AsyncIterator[Message]:
        producer_tasks.append(asyncio.current_task())
        yield started
        await child_started.wait()
        if failed_producer == "parent":
            raise error
        await wait_for_cancellation()

    async def child(*args: Any) -> None:
        producer_tasks.append(asyncio.current_task())
        child_started.set()
        if failed_producer == "child":
            raise error
        await wait_for_cancellation()

    monkeypatch.setattr(oc, "_iter_response", parent)
    monkeypatch.setattr(client, "_drain_child_session", child)
    messages = []

    async def consume() -> None:
        async for message in client.receive_response():
            messages.append(message)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not consumer.done()
        assert not cleaned.is_set()
        finish_cleanup.set()
        with pytest.raises(CLIConnectionError) as caught:
            await asyncio.wait_for(consumer, 1)
        assert caught.value is error
        assert cleaned.is_set()
        assert messages == [started]
        assert len(producer_tasks) == 2
        assert all(task.done() for task in producer_tasks)
    finally:
        finish_cleanup.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_during_cleanup", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("first_failed", [False, True])
async def test_receive_response_resumes_child_with_permissions(
    monkeypatch, make_client, resume_during_cleanup: bool, cancelled: bool,
    first_failed: bool,
) -> None:
    client = make_client()
    await client.connect()
    parent_id = client.session_id
    bus = client._bus
    client._options.can_use_tool = AsyncMock(return_value=PermissionResultAllow())
    approved = asyncio.Event()

    async def reply(*args: Any, **kwargs: Any) -> Mock:
        approved.set()
        return Mock(status_code=200)

    post = AsyncMock(side_effect=reply)
    monkeypatch.setattr(client._http, "post", post)
    sinks = [Mock(), Mock()]
    sink_factory = Mock(side_effect=sinks)
    monkeypatch.setattr(oc, "_ChildTranscriptSink", sink_factory)
    bridges = []
    bridge_ready = [asyncio.Event(), asyncio.Event()]
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    stopped = [asyncio.Event(), asyncio.Event()]
    create_bridge = client.create_permission_bridge

    def tracked_bridge(session_id: str) -> oc.PermissionBridge:
        index = len(bridges)
        if index == 1:
            assert stopped[0].is_set()
        bridge = create_bridge(session_id)
        stop = bridge.stop

        async def tracked_stop() -> None:
            if index == 0:
                cleanup_started.set()
                if resume_during_cleanup:
                    await finish_cleanup.wait()
            await stop()
            stopped[index].set()

        monkeypatch.setattr(bridge, "stop", tracked_stop)
        bridges.append(bridge)
        bridge_ready[index].set()
        return bridge

    monkeypatch.setattr(client, "create_permission_bridge", tracked_bridge)
    messages = []

    async def consume() -> None:
        async for message in client.receive_response():
            messages.append(message)

    def dispatch(event_type: str, session_id: str, **props: Any) -> None:
        bus._dispatch({
            "type": event_type,
            "properties": {"sessionID": session_id, **props},
        })

    def task_part(call_id: str, status: str) -> None:
        dispatch("message.part.updated", parent_id, part={
            "id": call_id, "type": "tool", "tool": "task", "callID": call_id,
            "state": {
                "status": status,
                "input": {"description": call_id, "task_id": "child" if call_id == "resume" else ""},
                "metadata": {"sessionId": "child"},
                "output": "done",
                "error": "child failed" if status == "error" else "",
            },
        })

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(2):
            task_part("initial", "running")
            task_part("initial", "running")
            await bridge_ready[0].wait()
            child_queue = bus._subscribers["child"]
            if first_failed:
                dispatch("session.error", "child", error={"message": "child failed"})
            else:
                dispatch("message.part.delta", "child", partID="first", field="text", delta="first answer")
            dispatch("session.idle", "child")
            task_part("initial", "error" if first_failed else "completed")
            await cleanup_started.wait()
            if not resume_during_cleanup:
                await stopped[0].wait()

            task_part("resume", "running")
            task_part("resume", "running")
            # These events must survive the gap between child invocations,
            # including an old bridge whose stop() has not finished yet.
            dispatch("message.part.updated", "child", part={
                "id": "read-part", "type": "tool", "tool": "read", "callID": "read-call",
                "state": {"status": "running", "input": {"filePath": "/workspace/file"}},
            })
            dispatch("permission.asked", "child", id="read-permission", permission="read",
                     tool={"callID": "read-call"})
            if resume_during_cleanup:
                await asyncio.sleep(0)
                assert len(bridges) == 1
                assert not approved.is_set()
            finish_cleanup.set()
            await bridge_ready[1].wait()
            await approved.wait()
            assert bus._subscribers["child"] is child_queue
            client._options.can_use_tool.assert_awaited_once()
            name, tool_input, context = client._options.can_use_tool.call_args.args
            assert (name, tool_input, context.tool_use_id) == (
                "read", {"filePath": "/workspace/file"}, "read-call",
            )
            assert bridges[1]._session_id == "child"
            post.assert_awaited_once_with(
                "/permission/read-permission/reply",
                params={"directory": "/workspace"}, json={"reply": "once"},
            )
            if cancelled:
                consumer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await consumer
            else:
                dispatch("message.part.delta", "child", partID="second", field="text", delta="second answer")
                dispatch("session.idle", "child")
                task_part("resume", "completed")
                dispatch("session.idle", parent_id)
                await consumer

            assert len(bridges) == 2
            assert all(event.is_set() for event in stopped)
            assert all(not bridge._tasks for bridge in bridges)
            assert set(bus._subscribers) == {parent_id}
            with pytest.raises(EventQueueClosed):
                await child_queue.get()
            assert client.session_id == parent_id
            starts = [message for message in messages if isinstance(message, TaskStartedMessage)]
            assert [message.tool_use_id for message in starts] == ["initial", "resume"]
            reads = [
                message for message in messages if isinstance(message, AssistantMessage)
                and any(isinstance(block, ToolUseBlock) and block.id == "read-call"
                        for block in message.content)
            ]
            assert len(reads) == 1
            assert reads[0].parent_tool_use_id == "resume"
            answers = [
                (message.parent_tool_use_id, block.text)
                for message in messages if isinstance(message, AssistantMessage)
                for block in message.content if isinstance(block, TextBlock)
            ]
            expected_answers = [] if first_failed else [("initial", "first answer")]
            if not cancelled:
                expected_answers.append(("resume", "second answer"))
            assert answers == expected_answers
            results = [message for message in messages if isinstance(message, ResultMessage)]
            assert [message.session_id for message in results] == ([] if cancelled else [parent_id])
            assert sink_factory.call_count == 2
            for sink in sinks:
                sink.close.assert_called_once()
    finally:
        finish_cleanup.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
