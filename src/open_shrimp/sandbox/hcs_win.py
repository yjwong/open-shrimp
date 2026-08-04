"""Windows-only plumbing for the HCS sandbox backend.

Wraps the ``win32more`` ctypes bindings for the Host Compute Service
(``computecore.dll``), the Host Compute Network service
(``computenetwork.dll``) and ``virtdisk.dll``, plus the two host↔guest
channels: the COM-port console named pipe and the ``AF_HYPERV`` control
socket.  Importable only on Windows — everything else lives in
:mod:`open_shrimp.sandbox.hcs_helpers`.

Binding conventions:

* HCS calls return ``S_OK`` synchronously even when the real work fails —
  the operative HRESULT and result document come from
  ``HcsWaitForOperationResult``; result documents are ``LocalAlloc``'d.
* HCN calls are synchronous; their out-strings are ``CoTaskMemAlloc``'d and
  freed with ``CoTaskMemFree``.
* HCN handle types are lazily generated and may be absent from the module —
  resolve via ``getattr`` with a bare ``c_void_p`` fallback; never
  instantiate ``HCN.HCN_*`` directly.
"""

from __future__ import annotations

import ctypes
import importlib
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from ctypes import wintypes
from typing import Any

from win32more import Guid
from win32more.Windows.Win32.Foundation import PWSTR
from win32more.Windows.Win32.System.HostComputeSystem import (
    HCS_SYSTEM,
    HcsCloseComputeSystem,
    HcsCloseOperation,
    HcsCreateComputeSystem,
    HcsCreateOperation,
    HcsEnumerateComputeSystems,
    HcsGrantVmAccess,
    HcsOpenComputeSystem,
    HcsStartComputeSystem,
    HcsTerminateComputeSystem,
    HcsWaitForOperationResult,
)

from open_shrimp.sandbox.hcs_helpers import vsock_service_id

logger = logging.getLogger(__name__)

_kernel32 = ctypes.windll.kernel32
_ole32 = ctypes.windll.ole32
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
_k32.ReadFile.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_GENERIC_ALL = 0x10000000
_OPEN_EXISTING = 3
_ERROR_BROKEN_PIPE = 109

_HCN = importlib.import_module(
    "win32more.Windows.Win32.System.HostComputeNetwork"
)


class HcsError(RuntimeError):
    """A failed HCS/HCN/virtdisk call, message pre-formatted with the raw
    HRESULT and any error record."""


def hrx(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


def hr_message(hr: int) -> str:
    if (hr & 0xFFFFFFFF) == 0:
        return "The operation completed successfully."
    try:
        return ctypes.FormatError(hr & 0xFFFFFFFF) or "<no message>"
    except Exception:
        return "<FormatMessage failed>"


def _take_local_str(p: PWSTR) -> str | None:
    if not p:
        return None
    text = p.value
    _kernel32.LocalFree(ctypes.cast(p, ctypes.c_void_p))
    return text


def _take_cotask_str(p: PWSTR) -> str | None:
    if not p:
        return None
    text = p.value
    _ole32.CoTaskMemFree(ctypes.cast(p, ctypes.c_void_p))
    return text


# ---------------------------------------------------------------------------
# HCS (compute systems)
# ---------------------------------------------------------------------------


class HcsOperation:
    """One ``HCS_OPERATION`` handle; reusable across sequential calls."""

    def __init__(self) -> None:
        self._op = HcsCreateOperation(None, None)
        if not self._op:
            raise HcsError("HcsCreateOperation returned NULL")

    def wait(self, what: str, timeout_ms: int = 60_000) -> tuple[int, str | None]:
        doc = PWSTR()
        hr = HcsWaitForOperationResult(self._op, timeout_ms, ctypes.byref(doc))
        text = _take_local_str(doc)
        logger.debug("hcs wait[%s]: %s doc=%s", what, hrx(hr), text)
        return hr, text

    @property
    def handle(self):
        return self._op

    def close(self) -> None:
        if self._op:
            HcsCloseOperation(self._op)
            self._op = None


class ComputeSystem:
    """An open ``HCS_SYSTEM`` handle."""

    def __init__(self, handle) -> None:
        self._h = handle

    @property
    def handle(self):
        return self._h

    def close(self) -> None:
        if self._h:
            HcsCloseComputeSystem(self._h)
            self._h = None


def create_compute_system(
    system_id: str, config_json: str, op: HcsOperation, timeout_ms: int = 60_000,
) -> ComputeSystem:
    handle = HCS_SYSTEM()
    hr = HcsCreateComputeSystem(
        system_id, config_json, op.handle, None, ctypes.byref(handle),
    )
    if hr == 0:
        hr, doc = op.wait(f"create {system_id}", timeout_ms)
    else:
        doc = None
    if hr != 0:
        if handle:
            HcsCloseComputeSystem(handle)
        raise HcsError(
            f"HcsCreateComputeSystem({system_id}) failed {hrx(hr)} "
            f"({hr_message(hr)}) doc={doc}"
        )
    return ComputeSystem(handle)


def start_compute_system(
    system: ComputeSystem, op: HcsOperation, timeout_ms: int = 60_000,
) -> None:
    hr = HcsStartComputeSystem(system.handle, op.handle, None)
    if hr == 0:
        hr, doc = op.wait("start", timeout_ms)
    else:
        doc = None
    if hr != 0:
        raise HcsError(
            f"HcsStartComputeSystem failed {hrx(hr)} ({hr_message(hr)}) "
            f"doc={doc}"
        )


def terminate_compute_system(
    system: ComputeSystem, op: HcsOperation, timeout_ms: int = 60_000,
) -> None:
    hr = HcsTerminateComputeSystem(system.handle, op.handle, None)
    if hr == 0:
        hr, doc = op.wait("terminate", timeout_ms)
    else:
        doc = None
    if hr != 0:
        raise HcsError(
            f"HcsTerminateComputeSystem failed {hrx(hr)} ({hr_message(hr)}) "
            f"doc={doc}"
        )


def grant_vm_access(system_id: str, file_path: str) -> None:
    """Grant the compute system's virtual SID access to *file_path*.

    A VHDX under a profile-restricted directory (LocalAppData, %TEMP%) is not
    readable by the VM worker's ``NT VIRTUAL MACHINE\\<vmid>`` identity, so the
    disk attachment fails to power on with ``0x80070005``.  This adds the ACE
    the VM worker needs, along the whole path — the same call hcsshim makes
    before attaching a LCOW disk.
    """
    hr = HcsGrantVmAccess(system_id, file_path)
    if hr != 0:
        raise HcsError(
            f"HcsGrantVmAccess({system_id}, {file_path}) failed {hrx(hr)} "
            f"({hr_message(hr)})"
        )


def open_compute_system(system_id: str) -> ComputeSystem:
    handle = HCS_SYSTEM()
    hr = HcsOpenComputeSystem(system_id, _GENERIC_ALL, ctypes.byref(handle))
    if hr != 0:
        raise HcsError(
            f"HcsOpenComputeSystem({system_id}) failed {hrx(hr)} "
            f"({hr_message(hr)})"
        )
    return ComputeSystem(handle)


def enumerate_compute_systems(
    owner: str, op: HcsOperation, timeout_ms: int = 60_000,
) -> list[dict[str, Any]]:
    query = json.dumps({"Owners": [owner]})
    hr = HcsEnumerateComputeSystems(query, op.handle)
    if hr == 0:
        hr, doc = op.wait("enumerate", timeout_ms)
    else:
        doc = None
    if hr != 0:
        raise HcsError(
            f"HcsEnumerateComputeSystems failed {hrx(hr)} ({hr_message(hr)})"
        )
    if not doc:
        return []
    return list(json.loads(doc))


# ---------------------------------------------------------------------------
# HCN (networks and endpoints)
# ---------------------------------------------------------------------------


def _hcn_handle(type_name: str):
    """HCN handle types may be absent from the generated module entirely;
    a bare void pointer is the working fallback."""
    t = getattr(_HCN, type_name, None)
    if t is None:
        return ctypes.c_void_p()
    try:
        return t()
    except (TypeError, AttributeError):
        return ctypes.c_void_p()


def _hcn_check(hr: int, what: str, error_record: str | None) -> None:
    if hr != 0:
        raise HcsError(
            f"{what} failed {hrx(hr)} ({hr_message(hr)}) "
            f"record={error_record}"
        )


def hcn_enumerate_networks() -> list[str]:
    out = PWSTR()
    err = PWSTR()
    hr = _HCN.HcnEnumerateNetworks("", ctypes.byref(out), ctypes.byref(err))
    otext = _take_cotask_str(out)
    etext = _take_cotask_str(err)
    _hcn_check(hr, "HcnEnumerateNetworks", etext)
    return list(json.loads(otext)) if otext else []


def hcn_create_network(guid_str: str, settings: str) -> None:
    g = Guid(guid_str)
    h = _hcn_handle("HCN_NETWORK")
    err = PWSTR()
    hr = _HCN.HcnCreateNetwork(
        ctypes.byref(g), settings, ctypes.byref(h), ctypes.byref(err),
    )
    etext = _take_cotask_str(err)
    _hcn_check(hr, f"HcnCreateNetwork({guid_str})", etext)
    _HCN.HcnCloseNetwork(h)


def hcn_network_exists(guid_str: str) -> bool:
    g = Guid(guid_str)
    h = _hcn_handle("HCN_NETWORK")
    err = PWSTR()
    hr = _HCN.HcnOpenNetwork(ctypes.byref(g), ctypes.byref(h), ctypes.byref(err))
    _take_cotask_str(err)
    if hr != 0:
        return False
    _HCN.HcnCloseNetwork(h)
    return True


def hcn_network_prefixes() -> list[ipaddress.IPv4Network]:
    """Every pre-existing network's IPv4 prefixes, for collision-checked
    subnet selection."""
    nets: list[ipaddress.IPv4Network] = []
    for nid in hcn_enumerate_networks():
        g = Guid(nid)
        h = _hcn_handle("HCN_NETWORK")
        err = PWSTR()
        hr = _HCN.HcnOpenNetwork(ctypes.byref(g), ctypes.byref(h), ctypes.byref(err))
        _take_cotask_str(err)
        if hr != 0:
            continue
        props = PWSTR()
        err = PWSTR()
        hr = _HCN.HcnQueryNetworkProperties(
            h, "", ctypes.byref(props), ctypes.byref(err),
        )
        ptext = _take_cotask_str(props)
        _take_cotask_str(err)
        _HCN.HcnCloseNetwork(h)
        if hr != 0 or not ptext:
            continue
        for p in set(re.findall(r"\d+\.\d+\.\d+\.\d+/\d+", ptext)):
            try:
                nets.append(ipaddress.ip_network(p, strict=False))
            except ValueError:
                pass
    return nets


def hcn_create_endpoint(
    network_guid: str, endpoint_guid: str, settings: str,
) -> None:
    ng = Guid(network_guid)
    nh = _hcn_handle("HCN_NETWORK")
    err = PWSTR()
    hr = _HCN.HcnOpenNetwork(ctypes.byref(ng), ctypes.byref(nh), ctypes.byref(err))
    etext = _take_cotask_str(err)
    _hcn_check(hr, f"HcnOpenNetwork({network_guid})", etext)
    try:
        eg = Guid(endpoint_guid)
        eh = _hcn_handle("HCN_ENDPOINT")
        err = PWSTR()
        hr = _HCN.HcnCreateEndpoint(
            nh, ctypes.byref(eg), settings, ctypes.byref(eh), ctypes.byref(err),
        )
        etext = _take_cotask_str(err)
        _hcn_check(hr, f"HcnCreateEndpoint({endpoint_guid})", etext)
        _HCN.HcnCloseEndpoint(eh)
    finally:
        _HCN.HcnCloseNetwork(nh)


def hcn_endpoint_properties(endpoint_guid: str) -> dict[str, Any] | None:
    g = Guid(endpoint_guid)
    h = _hcn_handle("HCN_ENDPOINT")
    err = PWSTR()
    hr = _HCN.HcnOpenEndpoint(ctypes.byref(g), ctypes.byref(h), ctypes.byref(err))
    _take_cotask_str(err)
    if hr != 0:
        return None
    props = PWSTR()
    err = PWSTR()
    hr = _HCN.HcnQueryEndpointProperties(h, "", ctypes.byref(props), ctypes.byref(err))
    ptext = _take_cotask_str(props)
    _take_cotask_str(err)
    _HCN.HcnCloseEndpoint(h)
    if hr != 0 or not ptext:
        return None
    return json.loads(ptext)


def hcn_delete_endpoint(endpoint_guid: str) -> None:
    g = Guid(endpoint_guid)
    err = PWSTR()
    hr = _HCN.HcnDeleteEndpoint(ctypes.byref(g), ctypes.byref(err))
    etext = _take_cotask_str(err)
    if hr != 0:
        logger.debug("HcnDeleteEndpoint(%s): %s %s", endpoint_guid, hrx(hr), etext)


def hcn_delete_network(network_guid: str) -> None:
    g = Guid(network_guid)
    err = PWSTR()
    hr = _HCN.HcnDeleteNetwork(ctypes.byref(g), ctypes.byref(err))
    etext = _take_cotask_str(err)
    if hr != 0:
        logger.debug("HcnDeleteNetwork(%s): %s %s", network_guid, hrx(hr), etext)


# ---------------------------------------------------------------------------
# virtdisk (persistent-volume VHDX creation)
# ---------------------------------------------------------------------------


def create_dynamic_vhdx(path: str, size_gb: int) -> None:
    """Create an empty dynamic (sparse) VHDX at *path* via ``virtdisk.dll``.

    The guest formats it (ext4) over the control channel — no host-side
    filesystem work, and ``computestorage.dll`` stays out entirely (it only
    knows NTFS container layers).
    """
    from win32more.Windows.Win32.Storage.Vhd import (
        CREATE_VIRTUAL_DISK_PARAMETERS,
        CreateVirtualDisk,
        VIRTUAL_STORAGE_TYPE,
    )

    storage_type = VIRTUAL_STORAGE_TYPE()
    storage_type.DeviceId = 3  # VIRTUAL_STORAGE_TYPE_DEVICE_VHDX
    storage_type.VendorId = Guid("EC984AEC-A0F9-47E9-901F-71415A66345B")

    params = CREATE_VIRTUAL_DISK_PARAMETERS()
    params.Version = 2  # CREATE_VIRTUAL_DISK_VERSION_2
    params.Anonymous.Version2.MaximumSize = size_gb * 1024 * 1024 * 1024
    params.Anonymous.Version2.BlockSizeInBytes = 0
    params.Anonymous.Version2.SectorSizeInBytes = 512
    params.Anonymous.Version2.PhysicalSectorSizeInBytes = 0

    handle = ctypes.c_void_p()
    # Version-2 parameters require VIRTUAL_DISK_ACCESS_NONE (0).
    rc = CreateVirtualDisk(
        ctypes.byref(storage_type),
        path,
        0,
        None,
        0,  # CREATE_VIRTUAL_DISK_FLAG_NONE → dynamic
        0,
        ctypes.byref(params),
        None,
        ctypes.byref(handle),
    )
    if rc != 0:
        raise HcsError(
            f"CreateVirtualDisk({path}) failed win32={rc} "
            f"({hr_message(rc)})"
        )
    _k32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Console reader (COM-port named pipe; the VM worker is the pipe server)
# ---------------------------------------------------------------------------


class ConsoleReader:
    """Background reader for the guest console named pipe.

    Start it *before* ``HcsCreateComputeSystem`` so no console bytes are
    lost; a broken pipe (guest terminated) is the clean exit signal.
    """

    def __init__(self, pipe_name: str) -> None:
        self._pipe_name = pipe_name
        self.buf = bytearray()
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        handle = None
        try:
            while not self._stop.is_set():
                handle = _k32.CreateFileW(
                    self._pipe_name, _GENERIC_READ | _GENERIC_WRITE, 0, None,
                    _OPEN_EXISTING, 0, None,
                )
                if handle is not None and handle != _INVALID_HANDLE_VALUE:
                    break
                handle = None
                time.sleep(0.05)
            if handle is None:
                return
            buf = ctypes.create_string_buffer(4096)
            n = wintypes.DWORD()
            while True:
                ok = _k32.ReadFile(handle, buf, len(buf), ctypes.byref(n), None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err != _ERROR_BROKEN_PIPE:
                        self.error = f"ReadFile: {err}"
                    return
                if n.value:
                    self.buf += buf.raw[: n.value]
        finally:
            if handle is not None:
                _k32.CloseHandle(handle)

    def wait_for(self, marker: bytes, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.buf:
                return True
            if self.error:
                return False
            time.sleep(0.05)
        return marker in self.buf

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)


# ---------------------------------------------------------------------------
# Control channel (AF_HYPERV → in-guest vsock agent)
# ---------------------------------------------------------------------------


class ControlChannel:
    """The host side of the initramfs agent's one-command-per-connection
    protocol: connect, write one newline-terminated command, read the
    combined output to EOF."""

    def __init__(self, runtime_id: str, port: int) -> None:
        self._vm_id = runtime_id
        self._service = vsock_service_id(port)

    def run(
        self,
        command: str,
        *,
        connect_timeout: float = 6.0,
        read_timeout: float = 60.0,
        tolerate_read_timeout: bool = False,
    ) -> tuple[bool, str]:
        """Run *command* in the guest; return ``(ok, combined_output)``.

        ``tolerate_read_timeout`` exists for commands that background a
        daemon: the agent's read-to-EOF framing never EOFs while the daemon
        holds the stream, so the caller times out deliberately and verifies
        the daemon separately.
        """
        if "\n" in command:
            raise ValueError("control commands are single-line")
        s = socket.socket(
            socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW,
        )
        data = b""
        try:
            try:
                s.setsockopt(
                    socket.HV_PROTOCOL_RAW,
                    socket.HVSOCKET_CONNECT_TIMEOUT,
                    int(connect_timeout * 1000),
                )
            except (AttributeError, OSError):
                pass
            s.settimeout(connect_timeout)
            s.connect((self._vm_id, self._service))
            s.sendall(command.encode() + b"\n")
            s.settimeout(read_timeout)
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return True, data.decode("utf-8", "replace")
        except TimeoutError:
            if tolerate_read_timeout:
                return True, data.decode("utf-8", "replace")
            return False, data.decode("utf-8", "replace")
        except OSError as e:
            logger.debug("control channel error for %r: %s", command, e)
            return False, data.decode("utf-8", "replace")
        finally:
            s.close()

    def ping(self) -> bool:
        ok, out = self.run("echo openshrimp-ping", connect_timeout=3.0,
                           read_timeout=10.0)
        return ok and "openshrimp-ping" in out


# ---------------------------------------------------------------------------
# Guest→host relay (host half): AF_HYPERV accept → 127.0.0.1:<port> bridge
# ---------------------------------------------------------------------------

_GUEST_COMM_KEY = (
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization"
    r"\GuestCommunicationServices"
)

_host_relay_lock = threading.Lock()
_host_relay_vms: set[str] = set()
_host_relay_registered = False


def register_guest_comm_service(port: int, name: str) -> None:
    """Register the vsock service GUID so guests may connect to a host app.

    The ``GuestCommunicationServices`` hive is the guest→host allow-list; a
    host ``AF_HYPERV`` listener is only reachable from a guest once its
    service GUID is registered here.  Idempotent; needs HKLM write (the bot
    already runs with the Hyper-V-admin token HCS requires).
    """
    import winreg

    guid = vsock_service_id(port)
    with winreg.CreateKey(
        winreg.HKEY_LOCAL_MACHINE, f"{_GUEST_COMM_KEY}\\{guid}",
    ) as key:
        winreg.SetValueEx(key, "ElementName", 0, winreg.REG_SZ, name)


def _relay_pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _relay_handle(conn: socket.socket) -> None:
    upstream = None
    try:
        hdr = b""
        while len(hdr) < 2:
            chunk = conn.recv(2 - len(hdr))
            if not chunk:
                return
            hdr += chunk
        port = (hdr[0] << 8) | hdr[1]
        upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        conn.close()
        return
    t = threading.Thread(target=_relay_pipe, args=(conn, upstream), daemon=True)
    t.start()
    _relay_pipe(upstream, conn)
    conn.close()
    upstream.close()


def _relay_accept(srv: socket.socket) -> None:
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            return
        threading.Thread(target=_relay_handle, args=(conn,), daemon=True).start()


def ensure_host_relay(runtime_id: str) -> None:
    """Start the host relay listener for one VM (once per RuntimeId).

    The relay service GUID is registered once (process-wide); the
    ``AF_HYPERV`` listener must bind to the VM's **RuntimeId** — a wildcard or
    children VmId is never routed guest connections on this stack, only the
    per-VM RuntimeId is.  Every sandbox guest reaches host loopback services
    through its own listener.
    """
    global _host_relay_registered
    from open_shrimp.sandbox.hcs_helpers import RELAY_PORT

    with _host_relay_lock:
        if not _host_relay_registered:
            register_guest_comm_service(RELAY_PORT, "openshrimp-hcs-relay")
            _host_relay_registered = True
        if runtime_id in _host_relay_vms:
            return
        srv = socket.socket(
            socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW,
        )
        srv.bind((runtime_id, vsock_service_id(RELAY_PORT)))
        srv.listen(64)
        threading.Thread(target=_relay_accept, args=(srv,), daemon=True).start()
        _host_relay_vms.add(runtime_id)
        logger.info(
            "HCS host relay listening for VM %s on hvsocket port 0x%x",
            runtime_id, RELAY_PORT,
        )


def vsock_port_reachable(runtime_id: str, port: int, timeout: float = 3.0) -> bool:
    """True when something in the guest accepts on vsock *port*."""
    s = socket.socket(
        socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW,
    )
    try:
        try:
            s.setsockopt(
                socket.HV_PROTOCOL_RAW,
                socket.HVSOCKET_CONNECT_TIMEOUT,
                int(timeout * 1000),
            )
        except (AttributeError, OSError):
            pass
        s.settimeout(timeout)
        s.connect((runtime_id, vsock_service_id(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()
