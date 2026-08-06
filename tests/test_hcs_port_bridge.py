"""The HCS host→guest bridge: both halves, ``reach()``, and port forwards.

The real bridge crosses hvsocket, but the two halves are separable: the guest
half takes an already-connected socket, and the host half takes an injected
dial.  Both are exercised here against a plain loopback listener standing in
for the guest service — no Windows, no vsock, no guest.

``reach()`` and the port-forward group are driven with a fake ``hcs_win``
module (the same shape the computer-use tests use), so the bookkeeping is
tested without binding anything.
"""

from __future__ import annotations

import re
import socket
import sys
import threading
import time

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs as hcs_mod
from open_shrimp.sandbox import hcs_guest_bridge as G
from open_shrimp.sandbox import hcs_helpers as H
from open_shrimp.sandbox import hcs_port_bridge as B
from open_shrimp.sandbox.hcs import HcsSandbox
from open_shrimp.sandbox.port_forward import pick_free_host_port

RID = "11111111-2222-3333-4444-555555555555"


# -- loopback stand-in for the guest service ---------------------------------


def _echo(conn: socket.socket) -> None:
    """Echo back upper-cased, so a reply proves it crossed the service."""
    with conn:
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            conn.sendall(data.upper())


@pytest.fixture
def echo_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def accept_loop() -> None:
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            threading.Thread(target=_echo, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture(autouse=True)
def _close_leaked_bridges():
    yield
    for owner in {owner for owner, _port in list(B._listeners)}:
        B.close_owner_bridges(owner)


def _bindable(port: int, timeout: float = 2.0) -> bool:
    """True once *port* can be bound again (the accept thread may still be
    unwinding when ``close_bridge`` returns)."""
    deadline = time.monotonic() + timeout
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        finally:
            probe.close()


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    sock.settimeout(5)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


# -- guest half: vsock connection → guest loopback ---------------------------


def test_guest_half_pipes_an_accepted_connection_to_the_guest_service(echo_port):
    host_end, vsock_end = socket.socketpair()
    worker = threading.Thread(
        target=G.bridge, args=(vsock_end, echo_port), daemon=True,
    )
    worker.start()

    host_end.sendall(b"hello")
    assert _recv_exactly(host_end, 5) == b"HELLO"

    # Half-close travels through: the service sees EOF, replies EOF, and the
    # bridge tears down on its own.
    host_end.shutdown(socket.SHUT_WR)
    assert _recv_exactly(host_end, 1) == b""
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_guest_half_closes_the_connection_when_nothing_listens():
    host_end, vsock_end = socket.socketpair()
    G.bridge(vsock_end, pick_free_host_port())
    assert _recv_exactly(host_end, 1) == b""


# -- host half: host loopback → injected dial --------------------------------


def test_host_half_binds_loopback_only_and_pipes_through_the_dial(echo_port):
    host_port = pick_free_host_port()
    B.open_bridge(
        "vm-a", host_port,
        lambda: socket.create_connection(("127.0.0.1", echo_port), timeout=5),
    )
    assert B._listeners[("vm-a", host_port)].getsockname() == (
        "127.0.0.1", host_port,
    )
    assert B.bridged_host_ports("vm-a") == [host_port]

    client = socket.create_connection(("127.0.0.1", host_port), timeout=5)
    client.sendall(b"ping")
    assert _recv_exactly(client, 4) == b"PING"
    client.close()


def test_both_halves_chained_reach_the_guest_service(echo_port):
    """Host listener → (socketpair standing in for hvsocket) → guest half."""
    host_side, guest_side = socket.socketpair()
    threading.Thread(
        target=G.bridge, args=(guest_side, echo_port), daemon=True,
    ).start()
    host_port = pick_free_host_port()
    B.open_bridge("vm-a", host_port, lambda: host_side)

    client = socket.create_connection(("127.0.0.1", host_port), timeout=5)
    client.sendall(b"end to end")
    assert _recv_exactly(client, 10) == b"END TO END"
    client.close()


def test_host_half_survives_a_failing_dial(echo_port):
    host_port = pick_free_host_port()

    def dial() -> socket.socket:
        raise OSError("guest listener is gone")

    B.open_bridge("vm-a", host_port, dial)
    client = socket.create_connection(("127.0.0.1", host_port), timeout=5)
    assert _recv_exactly(client, 1) == b""
    client.close()
    # The listener is still up; a later connection is served normally.
    assert B.bridged_host_ports("vm-a") == [host_port]


def test_open_bridge_is_idempotent_per_owner_and_host_port(echo_port):
    host_port = pick_free_host_port()
    dial = lambda: socket.create_connection(("127.0.0.1", echo_port), timeout=5)
    B.open_bridge("vm-a", host_port, dial)
    listener = B._listeners[("vm-a", host_port)]
    B.open_bridge("vm-a", host_port, dial)
    assert B._listeners[("vm-a", host_port)] is listener


def test_open_bridge_raises_when_the_host_port_is_taken():
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    try:
        with pytest.raises(OSError):
            B.open_bridge(
                "vm-a", squatter.getsockname()[1], lambda: squatter,
            )
        assert B.bridged_host_ports("vm-a") == []
    finally:
        squatter.close()


def test_close_bridge_frees_the_host_port(echo_port):
    host_port = pick_free_host_port()
    B.open_bridge(
        "vm-a", host_port,
        lambda: socket.create_connection(("127.0.0.1", echo_port), timeout=5),
    )
    assert B.close_bridge("vm-a", host_port) is True
    assert B.close_bridge("vm-a", host_port) is False
    assert B.bridged_host_ports("vm-a") == []
    # The port is bindable again, so neither the listener nor its accept
    # thread was left holding it.
    assert _bindable(host_port)


def test_close_owner_bridges_spares_other_owners(echo_port):
    dial = lambda: socket.create_connection(("127.0.0.1", echo_port), timeout=5)
    a_port, b_port = pick_free_host_port(), pick_free_host_port()
    B.open_bridge("vm-a", a_port, dial)
    B.open_bridge("vm-b", b_port, dial)
    B.close_owner_bridges("vm-a")
    assert B.bridged_host_ports("vm-a") == []
    assert B.bridged_host_ports("vm-b") == [b_port]


# -- reach() and port forwards on the sandbox --------------------------------


class _FakeControlChannel:
    """Records control commands; a launched guest listener then reports itself
    reachable, unless ``bind_ok`` is cleared."""

    commands: list[str] = []
    reachable: set[int] = set()
    bind_ok: bool = True

    def __init__(self, runtime_id: str, port: int) -> None:
        self.runtime_id = runtime_id
        self.port = port

    def run(self, command: str, **kwargs) -> tuple[bool, str]:
        type(self).commands.append(command)
        match = re.search(r"guest_bridge\.py (\d+)", command)
        if match and type(self).bind_ok:
            type(self).reachable.add(int(match.group(1)))
        return True, ""


def _fake_win(monkeypatch):
    """Install a fake ``hcs_win`` and return its recorded bridge calls."""
    opened: list[tuple[str, int, int]] = []
    closed: list[tuple[str, int]] = []
    closed_all: list[str] = []

    module = type(sys)("open_shrimp.sandbox.hcs_win")
    module.ControlChannel = _FakeControlChannel
    module.vsock_port_reachable = (
        lambda rid, port, timeout=3.0: port in _FakeControlChannel.reachable
    )
    module.open_guest_port_bridge = (
        lambda rid, host_port, guest_port: opened.append(
            (rid, host_port, guest_port)
        )
    )
    module.close_guest_port_bridge = (
        lambda rid, host_port: closed.append((rid, host_port)) or True
    )
    module.close_guest_port_bridges = closed_all.append
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", module)
    _FakeControlChannel.commands = []
    _FakeControlChannel.reachable = set()
    _FakeControlChannel.bind_ok = True
    return opened, closed, closed_all


def _make_sandbox(tmp_path, monkeypatch, **config_extra) -> HcsSandbox:
    monkeypatch.setattr(sys, "platform", "win32")
    defaults: dict = {
        "backend": "hcs",
        "base_image": str(tmp_path / "root.vhdx"),
    }
    defaults.update(config_extra)
    sb = HcsSandbox(
        "default", SandboxConfig(**defaults), str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
    )
    sb._runtime_id = RID
    return sb


def test_reach_starts_the_guest_listener_and_is_idempotent(tmp_path, monkeypatch):
    opened, _closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)

    endpoint = sb.reach(8080)
    host_port = int(endpoint.split(":")[1])
    assert endpoint == f"127.0.0.1:{host_port}"
    assert opened == [(RID, host_port, 8080)]

    # The in-guest listener is launched once, in the chroot, detached.
    assert len(_FakeControlChannel.commands) == 1
    command = _FakeControlChannel.commands[0]
    assert f"python3 {H.CHROOT_CFG_DIR}/guest_bridge.py 8080" in command
    assert f"chroot {H.MNT_ROOT}" in command
    assert command.rstrip("'").endswith("&")

    # Repeated calls return the same endpoint without stacking listeners.
    assert sb.reach(8080) == endpoint
    assert opened == [(RID, host_port, 8080)]
    assert len(_FakeControlChannel.commands) == 1

    # A second guest port gets its own listener and host port.
    other = sb.reach(9090)
    assert other != endpoint
    assert len(opened) == 2
    assert len(_FakeControlChannel.commands) == 2


def test_reach_adopts_a_guest_listener_left_by_an_earlier_run(
    tmp_path, monkeypatch,
):
    opened, _closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    _FakeControlChannel.reachable.add(8080)

    endpoint = sb.reach(8080)

    assert opened == [(RID, int(endpoint.split(":")[1]), 8080)]
    # Relaunching would only collide with the vsock port it already holds.
    assert _FakeControlChannel.commands == []


def test_reach_fails_when_the_guest_listener_never_binds(tmp_path, monkeypatch):
    opened, _closed, _all = _fake_win(monkeypatch)
    monkeypatch.setattr(hcs_mod.time, "sleep", lambda _seconds: None)
    _FakeControlChannel.bind_ok = False
    sb = _make_sandbox(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="never bound its vsock port"):
        sb.reach(8080)
    assert opened == []
    assert sb._reached_ports == {}


def test_reach_prefers_the_matching_host_port(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    guest_port = pick_free_host_port()
    assert sb.reach(guest_port) == f"127.0.0.1:{guest_port}"


def test_reach_requires_a_running_sandbox(tmp_path, monkeypatch):
    opened, _closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb._runtime_id = None
    with pytest.raises(RuntimeError, match="not running"):
        sb.reach(8080)
    assert opened == []


@pytest.mark.parametrize(
    "guest_port", [H.CONTROL_PORT, H.EXEC_PORT, H.RELAY_PORT, H.RDP_PORT,
                   H.P9_PORT_WORKSPACE],
)
def test_backend_channel_ports_cannot_be_bridged(
    tmp_path, monkeypatch, guest_port,
):
    opened, _closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="channels already occupies"):
        sb.reach(guest_port)
    assert opened == []


def test_additional_directory_share_ports_cannot_be_bridged(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb._additional_directories = [str(tmp_path / "extra")]
    with pytest.raises(RuntimeError, match="channels already occupies"):
        sb.reach(H.P9_PORT_EXTRA_BASE)


def test_port_forwarding_is_supported(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    assert _make_sandbox(tmp_path, monkeypatch).supports_port_forwarding()


def test_port_forward_lifecycle_per_scope(tmp_path, monkeypatch):
    opened, closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)

    requested = pick_free_host_port()
    one = sb.add_port_forward(3000, requested, "chat:1", "dev server")
    two = sb.add_port_forward(4000, None, "chat:1", None)
    other = sb.add_port_forward(3000, None, "chat:2", None)

    # A requested host port is honoured; None allocates.
    assert one.host_port == requested
    assert one.guest_port == 3000
    assert one.description == "dev server"
    assert two.host_port not in (0, one.host_port)
    assert {f.id for f in (one, two, other)} == {one.id, two.id, other.id}
    assert [(rid, gp) for rid, _hp, gp in opened] == [
        (RID, 3000), (RID, 4000), (RID, 3000),
    ]
    # Two forwards to the same guest port share the guest listener, not the
    # host one.
    assert other.host_port != one.host_port
    assert len(_FakeControlChannel.commands) == 2

    assert {f.id for f in sb.list_port_forwards()} == {one.id, two.id, other.id}
    assert {f.id for f in sb.list_port_forwards("chat:1")} == {one.id, two.id}
    assert [f.id for f in sb.list_port_forwards("chat:2")] == [other.id]

    assert sb.remove_port_forward(two.id) is True
    assert sb.remove_port_forward(two.id) is False
    assert sb.remove_port_forward("pf-nope") is False
    assert closed == [(RID, two.host_port)]
    assert {f.id for f in sb.list_port_forwards()} == {one.id, other.id}

    # Cleanup is scope-local.
    sb.cleanup_port_forwards("chat:1")
    assert closed[1:] == [(RID, one.host_port)]
    assert [f.id for f in sb.list_port_forwards()] == [other.id]

    sb.cleanup_port_forwards()
    assert closed[2:] == [(RID, other.host_port)]
    assert sb.list_port_forwards() == []


def test_port_forward_requires_a_running_sandbox(tmp_path, monkeypatch):
    opened, _closed, _all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb._runtime_id = None
    with pytest.raises(RuntimeError, match="not running"):
        sb.add_port_forward(3000, None, "chat:1", None)
    assert opened == []
    assert sb.list_port_forwards() == []


def test_reset_drops_every_bridge_and_its_bookkeeping(tmp_path, monkeypatch):
    _opened, _closed, closed_all = _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb.reach(8080)
    sb.add_port_forward(3000, None, "chat:1", None)

    sb._reset_guest_bridges()

    assert closed_all == [RID]
    assert sb.list_port_forwards() == []
    assert sb._reached_ports == {}
    assert sb._guest_bridge_ports == set()
    # A later reach() re-launches the guest listener rather than assuming the
    # dead boot's one is still there (the boot it belonged to is gone, so
    # nothing answers on that vsock port any more).
    _FakeControlChannel.commands.clear()
    _FakeControlChannel.reachable.clear()
    sb.reach(8080)
    assert len(_FakeControlChannel.commands) == 1
