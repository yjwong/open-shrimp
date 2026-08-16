"""Control channel: framing, endpoint naming, and a live socket round trip.

The Unix transport is the one exercised here because CI is Linux; the named
pipe shares every layer above the transport, so dispatch, error handling and
broadcast are covered for both.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from open_shrimp.control import ControlServer, CoreStatus, build_methods, endpoint_address
from open_shrimp.control import protocol
from open_shrimp.control import server as server_module


# -- Framing -----------------------------------------------------------------


def test_encode_is_newline_terminated():
    frame = protocol.encode({"id": 1, "method": "status"})
    assert frame.endswith(b"\n")
    assert b"\n" not in frame[:-1]


def test_decode_round_trip():
    message = {"id": 7, "result": {"state": "running"}}
    assert protocol.decode(protocol.encode(message).rstrip(b"\n")) == message


def test_decode_rejects_non_object():
    with pytest.raises(ValueError):
        protocol.decode(b"[1, 2, 3]")


# -- Endpoint naming ---------------------------------------------------------


def test_endpoint_address_is_instance_scoped():
    default = endpoint_address()
    named = endpoint_address("blue")
    assert default != named
    assert "blue" in named


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket layout")
def test_endpoint_address_is_a_socket_path_on_posix():
    assert endpoint_address().endswith("openshrimp.sock")


@pytest.mark.skipif(sys.platform == "win32", reason="sun_path is a POSIX limit")
@pytest.mark.asyncio
async def test_over_long_instance_name_is_refused_readably(status):
    """An address the kernel cannot hold must not surface as a bare EINVAL.

    The margin is tightest on macOS, where sun_path is 104 bytes and the
    address is /Users/<user>/Library/Caches/TemporaryItems/openshrimp/
    openshrimp-<instance>.sock.
    """
    # The instance name also introduces a separator, so a name this long puts
    # the address exactly one byte over.
    instance = "x" * (server_module.SUN_PATH_MAX - len(endpoint_address("")))
    assert len(endpoint_address(instance)) == server_module.SUN_PATH_MAX + 1

    srv = ControlServer(build_methods(status), instance_name=instance)
    with pytest.raises(RuntimeError, match="instance_name"):
        await srv.start()
    # Rejected before anything was created, so there is no stale node to clear.
    assert not Path(endpoint_address(instance)).exists()


# -- Live server -------------------------------------------------------------


@pytest.fixture
def status() -> CoreStatus:
    return CoreStatus(
        state="running",
        config_path="/tmp/config.yaml",
        contexts=["default", "work"],
        bot_username="testbot",
    )


class _Client:
    """Minimal newline-JSON client for the tests."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def request(self, method: str, **params) -> dict:
        frame = {"id": 1, "method": method}
        if params:
            frame["params"] = params
        self._writer.write(protocol.encode(frame))
        await self._writer.drain()
        return await self.recv()

    async def send_raw(self, payload: bytes) -> dict:
        self._writer.write(payload)
        await self._writer.drain()
        return await self.recv()

    async def recv(self) -> dict:
        line = await asyncio.wait_for(self._reader.readuntil(b"\n"), timeout=5)
        return protocol.decode(line)

    async def close(self) -> None:
        self._writer.close()


@pytest_asyncio.fixture
async def server(status, request):
    # Scope the endpoint to the test so a bot running on this machine is
    # neither disturbed nor mistaken for the fixture.
    instance = f"pytest-{os.getpid()}-{request.node.name[:20]}"
    srv = ControlServer(build_methods(status), instance_name=instance)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.shutdown()


@pytest_asyncio.fixture
async def client(server):
    reader, writer = await asyncio.open_unix_connection(server.address)
    conn = _Client(reader, writer)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_status_reports_the_core(client, status):
    reply = await client.request("status")
    result = reply["result"]
    assert reply["id"] == 1
    assert result["state"] == "running"
    assert result["contexts"] == ["default", "work"]
    assert result["bot_username"] == "testbot"
    assert result["pid"] == os.getpid()
    assert result["protocol"] == protocol.PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_status_follows_mutations(client, status):
    status.state = "error"
    status.error = "boom"
    result = (await client.request("status"))["result"]
    assert result["state"] == "error"
    assert result["error"] == "boom"


@pytest.mark.asyncio
async def test_unknown_method_is_an_error_not_a_disconnect(client):
    reply = await client.request("nope")
    assert reply["error"]["code"] == protocol.ERR_UNKNOWN_METHOD
    # The channel survives, so a UI probing an older core can keep going.
    assert (await client.request("status"))["result"]["state"] == "running"


@pytest.mark.asyncio
async def test_malformed_frame_is_reported(client):
    reply = await client.send_raw(b"not json\n")
    assert reply["error"]["code"] == protocol.ERR_BAD_REQUEST


@pytest.mark.asyncio
async def test_non_object_params_rejected(client):
    reply = await client.send_raw(
        (json.dumps({"id": 2, "method": "status", "params": []}) + "\n").encode()
    )
    assert reply["error"]["code"] == protocol.ERR_BAD_REQUEST


@pytest.mark.asyncio
async def test_shutdown_replies_before_unwinding(client, status, monkeypatch):
    called = asyncio.Event()
    monkeypatch.setattr("open_shrimp.main.request_shutdown", called.set)

    reply = await client.request("shutdown")

    assert reply["result"] == {"accepted": True}
    assert called.is_set()
    assert status.state == "stopping"


@pytest.mark.asyncio
async def test_restart_requests_both(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("open_shrimp.main.request_restart", lambda: calls.append("restart"))
    monkeypatch.setattr("open_shrimp.main.request_shutdown", lambda: calls.append("shutdown"))

    await client.request("restart")

    assert calls == ["restart", "shutdown"]


@pytest.mark.asyncio
async def test_broadcast_reaches_connected_clients(server, client):
    server.broadcast("stopping", {"reason": "restart"})
    frame = await client.recv()
    assert frame == {"event": "stopping", "data": {"reason": "restart"}}


@pytest.mark.asyncio
async def test_shutdown_does_not_stall_on_a_connected_client(status):
    """A supervising UI holds the channel open for events.

    Shutdown is the one path that must not stall: everything downstream of it
    releases the MCP proxy, the tunnel and the sandbox guests.
    """
    instance = f"pytest-hold-{os.getpid()}"
    srv = ControlServer(build_methods(status), instance_name=instance)
    await srv.start()

    reader, writer = await asyncio.open_unix_connection(srv.address)
    try:
        await asyncio.wait_for(srv.shutdown(), timeout=10)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_oversized_frame_is_reported_not_silently_dropped(client):
    """A bare disconnect is indistinguishable from the core dying."""
    reply = await client.send_raw(b"x" * (protocol.MAX_FRAME_BYTES + 10) + b"\n")
    assert reply["error"]["code"] == protocol.ERR_BAD_REQUEST


@pytest.mark.asyncio
async def test_socket_is_owner_only(server):
    mode = Path(server.address).stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.asyncio
async def test_shutdown_removes_the_socket(status):
    instance = f"pytest-stale-{os.getpid()}"
    srv = ControlServer(build_methods(status), instance_name=instance)
    await srv.start()
    path = Path(srv.address)
    assert path.exists()

    await srv.shutdown()
    assert not path.exists()


@pytest.mark.asyncio
async def test_distinct_instances_do_not_collide(status):
    """Two cores with different instance names each get their own endpoint."""
    a = ControlServer(build_methods(status), instance_name=f"pytest-a-{os.getpid()}")
    b = ControlServer(build_methods(status), instance_name=f"pytest-b-{os.getpid()}")
    await a.start()
    try:
        await b.start()
        try:
            assert a.address != b.address
            for srv in (a, b):
                reader, writer = await asyncio.open_unix_connection(srv.address)
                writer.close()
        finally:
            await b.shutdown()
    finally:
        await a.shutdown()


@pytest.mark.asyncio
async def test_same_instance_twice_is_refused(status):
    """A second core sharing an instance name must fail loudly, not shadow."""
    instance = f"pytest-dup-{os.getpid()}"
    first = ControlServer(build_methods(status), instance_name=instance)
    await first.start()
    try:
        second = ControlServer(build_methods(status), instance_name=instance)
        with pytest.raises(RuntimeError, match="instance_name"):
            await second.start()
        # The incumbent is untouched.
        reader, writer = await asyncio.open_unix_connection(first.address)
        writer.close()
    finally:
        await first.shutdown()


@pytest.mark.asyncio
async def test_start_reclaims_a_stale_socket(status):
    """A killed core leaves the socket node behind; the next start must bind."""
    instance = f"pytest-reclaim-{os.getpid()}"
    address = Path(endpoint_address(instance))
    address.parent.mkdir(parents=True, exist_ok=True)
    address.touch()

    srv = ControlServer(build_methods(status), instance_name=instance)
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(address))
        writer.close()
    finally:
        await srv.shutdown()
