"""Pure helpers for the Windows HCS sandbox backend.

Everything in this module is platform-neutral and importable on any OS —
no ``win32more``, no Windows API calls.  The Windows-only plumbing lives in
:mod:`open_shrimp.sandbox.hcs_win`; the backend composing both lives in
:mod:`open_shrimp.sandbox.hcs`.

The vocabulary here mirrors what the compute-system config JSON needs:

* the guest is booted via ``Chipset.LinuxKernelDirect`` (WSL-shipped kernel +
  a busybox initramfs carrying a static vsock agent),
* the workspace and agent home are ``Devices.Plan9`` shares the guest mounts
  by dialing each share's vsock port,
* the agent rootfs and every ``persistent_paths`` entry are dynamic VHDX
  disks attached under ``VirtualMachine.Devices.Scsi``,
* the control channel is an ``AF_HYPERV``/``AF_VSOCK`` socket pair enabled by
  an allow-all ``Devices.HvSocket`` security descriptor,
* networking is a dedicated HCN NAT network + endpoint referenced from
  ``Devices.NetworkAdapters``, with the static address pushed into the guest
  from the endpoint's properties (HNS serves no DHCP to VMs).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path, PureWindowsPath

# -- vsock port plan ---------------------------------------------------------

#: The initramfs control agent (one short command per connection).
CONTROL_PORT = 0x5000
#: The in-rootfs exec server (one long-lived CLI session per connection).
EXEC_PORT = 0x5001
#: The guest→host relay: the guest dials AF_VSOCK CID_HOST:this, the host
#: accepts on AF_HYPERV and bridges to a host-loopback TCP port.  This is how
#: the sandbox reaches host services (the MCP proxy) that bind 127.0.0.1 — the
#: HNS NAT gateway forwards guest egress but not inbound to host loopback.
RELAY_PORT = 0x5002
#: The guest RDP endpoint of a computer-use context: weston's rdp-backend on
#: TCP 127.0.0.1:3389 behind an in-guest ``socat VSOCK-LISTEN:3389`` relay,
#: dialed from the host over AF_HYPERV like every other vsock port.
RDP_PORT = 3389

#: Plan9 share vsock ports, in declaration order.
P9_PORT_WORKSPACE = 564
P9_PORT_HOME = 565
P9_PORT_CFG = 566
P9_PORT_TASK_TMP = 567
P9_PORT_EXTRA_BASE = 568

#: Guest-side mount points (outside the rootfs chroot).
MNT_WORKSPACE = "/mnt/ws"
MNT_HOME = "/mnt/home"
MNT_CFG = "/mnt/cfg"
MNT_TASK_TMP = "/mnt/tasktmp"
MNT_ROOT = "/mnt/root"

#: Where the cfg share (exec agent source, provision script, uploads)
#: surfaces inside the rootfs chroot.
CHROOT_CFG_DIR = "/run/openshrimp"

#: The ext4 volume label the operator's ``base_image`` VHDX is formatted with;
#: the guest resolves and mounts the rootfs by this label.  It is a disk
#: convention of the supplied image, not the identity of any agent.
ROOTFS_LABEL = "clauderoot"

#: Console marker printed by the initramfs agent once its vsock listener is
#: up; the cold-boot path waits for it before opening the control channel.
AGENT_MARKER = b"AGENT-LISTENING"

#: Marker the exec server prints (to its log file) once listening.
EXEC_AGENT_MARKER = "OPENSHRIMP-EXEC-AGENT-LISTENING"

#: PATH exported for everything run inside the rootfs chroot.
CHROOT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: HOME exported for everything run inside the rootfs chroot.  The chroot has
#: no unprivileged sandbox user — every guest process is root — so the agent's
#: home-relative layout is re-rooted here rather than at the runtime's own
#: guest home.
CHROOT_HOME = "/root"

#: Desktop bring-up script the computer-use rootfs template bakes in
#: (weston-rdp + the vsock relay, self-supervising); the provision pass
#: launches it detached once the guest is mounted.
GUI_START_SCRIPT = "/usr/local/bin/start-weston.sh"

#: The hvsocket service-GUID template: the vsock port number in the first
#: dword, the fixed vsock-facb suffix elsewhere.
def vsock_service_id(port: int) -> str:
    """Return the hvsocket service GUID addressing vsock *port*."""
    return "%08x-facb-11e6-bd58-64006a7986d3" % port


# -- path mapping ------------------------------------------------------------


def windows_to_guest_path(win_path: str) -> str:
    """Map a Windows host path to its guest-side mount point.

    Drops the drive letter and flips separators:
    ``C:\\Users\\me\\repo`` → ``/Users/me/repo``.

    The drive-relative form is deliberate: ``os.path.realpath`` on Windows
    resolves both the original host path and this POSIX form to the same
    ``<cwd drive>:\\...`` string, so the approval layer's
    path-inside-directory checks accept guest paths without a mapping table
    (holds when the workspace lives on the process's current drive).
    """
    p = PureWindowsPath(win_path)
    parts = p.parts
    if p.drive:
        parts = parts[1:]
    return "/" + "/".join(parts)


def gui_image_path(base_image: str) -> str:
    """Path of the computer-use rootfs template variant that lives next to
    the operator's ``base_image`` (``root.vhdx`` → ``root-gui.vhdx``),
    baked by ``scripts/build_hcs_gui_rootfs.sh``."""
    p = PureWindowsPath(base_image)
    return str(p.with_name(p.stem + "-gui" + p.suffix))


def persistent_vol_label(guest_path: str) -> str:
    """ext4 label for a persistent volume — same scheme as the libvirt
    backend (``pv-`` + first 8 hex chars of SHA-256 of the guest path; ext4
    labels max out at 16 chars)."""
    h = hashlib.sha256(guest_path.encode()).hexdigest()[:8]
    return f"pv-{h}"


def persistent_vol_filename(guest_path: str) -> str:
    """VHDX filename for a persistent volume guest path."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", guest_path).strip("_")[:40]
    h = hashlib.sha256(guest_path.encode()).hexdigest()[:8]
    return f"pv-{slug}-{h}.vhdx"


def persistent_dev_name(idx: int) -> str:
    """Guest block device for persistent volume *idx*.

    The rootfs VHDX rides SCSI LUN 0 (``/dev/sda``); persistent volumes
    occupy LUNs 1.. in ``persistent_paths`` order.  The device name is only
    the *formatting* target for a blank disk — a formatted volume is always
    resolved by its ext4 label, so attach-order drift cannot corrupt data.
    """
    return f"sd{chr(ord('b') + idx)}"


# -- HCN JSON ----------------------------------------------------------------


def pick_subnet(taken: list[ipaddress.IPv4Network]) -> ipaddress.IPv4Network:
    """Pick a /24 that overlaps none of the pre-existing HNS networks."""
    for third in (222, 223, 199, 173, 151, 137, 118, 97):
        cand = ipaddress.ip_network(f"192.168.{third}.0/24")
        if not any(cand.overlaps(t) for t in taken):
            return cand
    raise RuntimeError(
        "no free 192.168.x.0/24 candidate subnet — every candidate overlaps "
        "an existing HNS network"
    )


def compose_network_settings(name: str, subnet: ipaddress.IPv4Network) -> str:
    """HCN settings JSON for a dedicated NAT network."""
    gateway = str(next(subnet.hosts()))
    return json.dumps({
        "SchemaVersion": {"Major": 2, "Minor": 0},
        "Name": name,
        "Type": "NAT",
        "Ipams": [{
            "Type": "Static",
            "Subnets": [{
                "IpAddressPrefix": str(subnet),
                "Routes": [{
                    "NextHop": gateway,
                    "DestinationPrefix": "0.0.0.0/0",
                }],
            }],
        }],
        "Flags": 0,
    })


def compose_endpoint_settings(name: str, network_guid: str) -> str:
    """HCN settings JSON for the per-context endpoint."""
    return json.dumps({
        "SchemaVersion": {"Major": 2, "Minor": 0},
        "Name": name,
        "HostComputeNetwork": network_guid,
        "Policies": [],
    })


# -- compute-system JSON -----------------------------------------------------

#: The boot log stays at loglevel=4: the emulated UART drains a full
#: loglevel=7 dmesg at ~300 B/s, which would dominate cold start.
KERNEL_CMDLINE = (
    "console=ttyS0,115200 "
    "8250_core.nr_uarts=1 8250_core.skip_txen_test=1 panic=-1 loglevel=4"
)


def compose_vm_config(
    *,
    owner: str,
    kernel_path: str,
    initrd_path: str,
    memory_mb: int,
    cpus: int,
    console_pipe: str,
    endpoint_guid: str,
    endpoint_mac: str,
    p9_shares: list[tuple[str, str, int, int]],
    scsi_disks: list[str],
    connect_sddl: str = "D:P(A;;FA;;;WD)",
    schema_minor: int = 1,
) -> dict:
    """Compose the full compute-system config JSON.

    *p9_shares* is ``(name, host_path, port, flags)`` per share (flags 1 =
    read-only, enforced by the 9p server in the VM worker).  *scsi_disks* is
    the ordered VHDX host-path list — index = SCSI LUN, so the rootfs must be
    first.  ``ShouldTerminateOnLastHandleClosed`` is false so the guest
    outlives the bot process; teardown is always an explicit terminate.
    """
    return {
        "SchemaVersion": {"Major": 2, "Minor": schema_minor},
        "Owner": owner,
        "ShouldTerminateOnLastHandleClosed": False,
        "VirtualMachine": {
            "StopOnReset": True,
            "Chipset": {
                "LinuxKernelDirect": {
                    "KernelFilePath": kernel_path,
                    "InitRdPath": initrd_path,
                    "KernelCmdLine": KERNEL_CMDLINE,
                }
            },
            "ComputeTopology": {
                "Memory": {"SizeInMB": memory_mb, "Backing": "Virtual"},
                "Processor": {"Count": cpus},
            },
            "Devices": {
                "ComPorts": {"0": {"NamedPipe": console_pipe}},
                # A permissive connect SD is required for the host to address
                # the guest's hvsocket endpoints at all (without one the
                # control channel fails WSAEADDRNOTAVAIL); *connect_sddl*
                # narrows it to the bot's own account so no other local user
                # can drive the guest's exec/control ports.  The bind SD (the
                # guest→host relay direction) stays allow-all.
                "HvSocket": {
                    "HvSocketConfig": {
                        "DefaultBindSecurityDescriptor": "D:P(A;;FA;;;WD)",
                        "DefaultConnectSecurityDescriptor": connect_sddl,
                    }
                },
                "Plan9": {
                    "Shares": [
                        {
                            "Name": name,
                            "AccessName": name,
                            "Path": host_path,
                            "Port": port,
                            "Flags": flags,
                        }
                        for name, host_path, port, flags in p9_shares
                    ]
                },
                "NetworkAdapters": {
                    endpoint_guid: {
                        "EndpointId": endpoint_guid,
                        "MacAddress": endpoint_mac,
                    }
                },
                "Scsi": {
                    "0": {
                        "Attachments": {
                            str(lun): {"Type": "VirtualDisk", "Path": path}
                            for lun, path in enumerate(scsi_disks)
                        }
                    }
                },
            },
        },
    }


# -- fingerprints ------------------------------------------------------------


def _file_stamp(path: Path) -> list:
    try:
        st = path.stat()
    except OSError:
        return [str(path), 0, 0]
    return [str(path), st.st_size, int(st.st_mtime)]


def config_fingerprint(
    *,
    kernel_path: Path,
    initrd_path: Path,
    base_image: str | None,
    project_dir: str,
    additional_directories: list[str],
    persistent_paths: list[str],
    memory_mb: int,
    cpus: int,
    provision: str | None,
    computer_use: bool = False,
) -> str:
    """SHA-256 over every input whose drift requires terminate + recreate.

    Per-boot values (console pipe name, endpoint GUID/MAC, RuntimeId) are
    deliberately excluded — they change on every create without invalidating
    anything.  ``computer_use`` is included because flipping it swaps the
    rootfs template (GUI vs non-GUI) under a possibly-running guest, which
    is only safe through the terminate + recreate path.
    """
    doc = {
        "kernel": _file_stamp(kernel_path),
        "initrd": _file_stamp(initrd_path),
        "base_image": base_image or "",
        "project_dir": project_dir,
        "additional_directories": list(additional_directories),
        "persistent_paths": list(persistent_paths),
        "memory_mb": memory_mb,
        "cpus": cpus,
        "provision": provision or "",
        "computer_use": bool(computer_use),
    }
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True).encode()
    ).hexdigest()


def rootfs_fingerprint(template_image: str, *, gui: bool = False) -> str:
    """Identity of the rootfs template a context's ``rootfs.vhdx`` was copied
    from.  Drift → the copy is re-seeded (agent state inside it is lost;
    ``persistent_paths`` volumes are untouched).  ``gui`` marks which
    template variant (computer-use desktop vs plain) the copy came from, so
    flipping ``computer_use`` re-seeds even if both variants share one file.
    """
    doc = [_file_stamp(Path(template_image)), "gui" if gui else "base"]
    return hashlib.sha256(json.dumps(doc).encode()).hexdigest()


# -- launcher (cli_path) -----------------------------------------------------

#: Exit codes the launcher uses for its own failures, chosen from the
#: sysexits range so they are distinguishable from CLI exit codes.
LAUNCHER_EXIT_PROTOCOL = 70
LAUNCHER_EXIT_CONNECT = 71

#: C# source for the per-context launcher executable the Agent SDK spawns as
#: ``cli_path``.  Compiled with the in-box .NET Framework ``csc.exe`` — a
#: ``.cmd`` shim is a command-injection sink for the SDK's own arguments,
#: and a compiled exe delivers argv byte-exact.  The launcher never
#: re-serialises argv through any shell: it ships it as a JSON list over
#: hvsocket to the in-guest exec server, which ``exec``\ s it directly.
#:
#: ``__LAUNCH_JSON__`` is replaced with the per-context launch-config path.
LAUNCHER_CS_TEMPLATE = r"""
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

static class OpenShrimpHcsLauncher
{
    const string LaunchConfigPath = @"__LAUNCH_JSON__";
    const int AF_HYPERV = 34;
    const int HV_PROTOCOL_RAW = 1;

    // SOCKADDR_HV: ushort family, ushort reserved, GUID VmId, GUID ServiceId.
    class HyperVEndPoint : EndPoint
    {
        readonly Guid vmId;
        readonly Guid serviceId;
        public HyperVEndPoint(Guid vm, Guid svc) { vmId = vm; serviceId = svc; }
        public override AddressFamily AddressFamily
        {
            get { return (AddressFamily)AF_HYPERV; }
        }
        public override SocketAddress Serialize()
        {
            var sa = new SocketAddress((AddressFamily)AF_HYPERV, 36);
            byte[] vm = vmId.ToByteArray();
            byte[] svc = serviceId.ToByteArray();
            for (int i = 0; i < 16; i++) sa[4 + i] = vm[i];
            for (int i = 0; i < 16; i++) sa[20 + i] = svc[i];
            return sa;
        }
        public override EndPoint Create(SocketAddress sa) { return this; }
    }

    // JavaScriptSerializer decodes JSON arrays as ArrayList when the target
    // is `object`, not object[] — accept any non-string enumerable.
    static IEnumerable<object> AsList(object o)
    {
        var list = new List<object>();
        if (o is System.Collections.IEnumerable && !(o is string))
            foreach (object x in (System.Collections.IEnumerable)o) list.Add(x);
        return list;
    }

    static bool ReadExact(Socket s, byte[] buf, int n)
    {
        int off = 0;
        while (off < n)
        {
            int got;
            try { got = s.Receive(buf, off, n - off, SocketFlags.None); }
            catch (SocketException) { return false; }
            catch (ObjectDisposedException) { return false; }
            if (got <= 0) return false;
            off += got;
        }
        return true;
    }

    static int Main(string[] args)
    {
        var ser = new JavaScriptSerializer();
        ser.MaxJsonLength = int.MaxValue;
        var cfg = ser.Deserialize<Dictionary<string, object>>(
            File.ReadAllText(LaunchConfigPath, Encoding.UTF8));

        string runtimeIdFile = (string)cfg["runtime_id_file"];
        int port = Convert.ToInt32(cfg["port"]);
        string cwd = (string)cfg["cwd"];
        double connectTimeout = cfg.ContainsKey("connect_timeout_s")
            ? Convert.ToDouble(cfg["connect_timeout_s"]) : 30.0;

        var env = new Dictionary<string, string>();
        object rawEnv;
        if (cfg.TryGetValue("env", out rawEnv) && rawEnv is Dictionary<string, object>)
            foreach (var kv in (Dictionary<string, object>)rawEnv)
                env[kv.Key] = Convert.ToString(kv.Value);
        object rawPass;
        if (cfg.TryGetValue("env_passthrough", out rawPass))
            foreach (object name in AsList(rawPass))
            {
                string v = Environment.GetEnvironmentVariable((string)name);
                if (!string.IsNullOrEmpty(v)) env[(string)name] = v;
            }

        var argv = new List<object>();
        object rawPrefix;
        if (cfg.TryGetValue("argv_prefix", out rawPrefix))
            argv.AddRange(AsList(rawPrefix));
        argv.AddRange(args);

        Guid serviceId = new Guid(string.Format(
            "{0:x8}-facb-11e6-bd58-64006a7986d3", port));

        Socket sock = null;
        Exception last = null;
        DateTime deadline = DateTime.UtcNow.AddSeconds(connectTimeout);
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                string vmId = File.ReadAllText(runtimeIdFile).Trim();
                var s = new Socket((AddressFamily)AF_HYPERV, SocketType.Stream,
                                   (ProtocolType)HV_PROTOCOL_RAW);
                s.Connect(new HyperVEndPoint(new Guid(vmId), serviceId));
                sock = s;
                break;
            }
            catch (Exception e) { last = e; Thread.Sleep(500); }
        }
        if (sock == null)
        {
            Console.Error.WriteLine(
                "openshrimp hcs launcher: sandbox unreachable: " + last);
            return __EXIT_CONNECT__;
        }

        var header = new Dictionary<string, object> {
            { "argv", argv }, { "cwd", cwd }, { "env", env },
        };
        byte[] headerBytes = Encoding.UTF8.GetBytes(ser.Serialize(header));
        byte[] lenBuf = new byte[4] {
            (byte)(headerBytes.Length >> 24), (byte)(headerBytes.Length >> 16),
            (byte)(headerBytes.Length >> 8), (byte)headerBytes.Length,
        };
        sock.Send(lenBuf);
        sock.Send(headerBytes);

        Stream stdinS = Console.OpenStandardInput();
        Stream stdoutS = Console.OpenStandardOutput();
        Stream stderrS = Console.OpenStandardError();

        var stdinPump = new Thread(() =>
        {
            byte[] buf = new byte[65536];
            try
            {
                int n;
                while ((n = stdinS.Read(buf, 0, buf.Length)) > 0)
                    sock.Send(buf, 0, n, SocketFlags.None);
            }
            catch (Exception) { }
            try { sock.Shutdown(SocketShutdown.Send); } catch (Exception) { }
        });
        stdinPump.IsBackground = true;
        stdinPump.Start();

        byte[] hdr = new byte[5];
        while (true)
        {
            if (!ReadExact(sock, hdr, 5)) break;
            int len = (hdr[1] << 24) | (hdr[2] << 16) | (hdr[3] << 8) | hdr[4];
            byte[] payload = new byte[len];
            if (len > 0 && !ReadExact(sock, payload, len)) break;
            if (hdr[0] == 1) { stdoutS.Write(payload, 0, len); stdoutS.Flush(); }
            else if (hdr[0] == 2) { stderrS.Write(payload, 0, len); stderrS.Flush(); }
            else if (hdr[0] == 3)
            {
                int code;
                if (!int.TryParse(Encoding.ASCII.GetString(payload), out code))
                    code = __EXIT_PROTOCOL__;
                return code;
            }
        }
        Console.Error.WriteLine(
            "openshrimp hcs launcher: connection dropped without exit status");
        return __EXIT_PROTOCOL__;
    }
}
"""


def render_launcher_source(launch_json_path: str) -> str:
    """Fill the launcher template with the per-context launch-config path."""
    # The path lands inside a C# verbatim string: only '"' needs doubling.
    escaped = launch_json_path.replace('"', '""')
    return (
        LAUNCHER_CS_TEMPLATE
        .replace("__LAUNCH_JSON__", escaped)
        .replace("__EXIT_CONNECT__", str(LAUNCHER_EXIT_CONNECT))
        .replace("__EXIT_PROTOCOL__", str(LAUNCHER_EXIT_PROTOCOL))
    )
