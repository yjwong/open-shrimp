"""HCS computer-use wiring: config validation for the hcs backend and the
sandbox's delegation to the host-side RDP session (mocked — no guest)."""

from __future__ import annotations

import io
import sys
import zipfile
from unittest.mock import MagicMock

import pytest

from open_shrimp.config import (
    SandboxConfig,
    _parse,
    _validate_raw,
    config_to_dict,
)
from open_shrimp.sandbox import hcs as hcs_mod
from open_shrimp.sandbox import hcs_rdp
from open_shrimp.sandbox.hcs import HcsSandbox

MINGW = r"C:\msys64\mingw64\bin"


def _hcs_raw(sandbox_extra: dict | None = None):
    sandbox: dict = {"backend": "hcs", "base_image": r"C:\images\root.vhdx"}
    if sandbox_extra:
        sandbox.update(sandbox_extra)
    return {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
                "sandbox": sandbox,
            }
        },
        "default_context": "default",
    }


# -- config validation --------------------------------------------------------


def test_hcs_computer_use_with_mingw_bin_validates():
    _validate_raw(_hcs_raw({"computer_use": True, "mingw_bin": MINGW}))


def test_hcs_computer_use_without_mingw_bin_validates():
    # The RDP helper ships prebuilt; a toolchain is only the build-from-source
    # fallback, so computer use does not require one.
    _validate_raw(_hcs_raw({"computer_use": True}))


def test_mingw_bin_must_be_a_string():
    with pytest.raises(ValueError, match="mingw_bin must be a string"):
        _validate_raw(_hcs_raw({"computer_use": True, "mingw_bin": 3}))


def test_mingw_bin_rejected_on_another_backend():
    raw = _hcs_raw()
    sandbox = raw["contexts"]["default"]["sandbox"]
    sandbox["backend"] = "docker"
    sandbox.pop("base_image")
    sandbox["mingw_bin"] = MINGW
    with pytest.raises(ValueError, match="applies only to the hcs backend"):
        _validate_raw(raw)


def test_hcs_without_computer_use_needs_no_mingw_bin():
    _validate_raw(_hcs_raw())


def test_mingw_bin_round_trips():
    cfg = _parse(_hcs_raw({"computer_use": True, "mingw_bin": MINGW}))
    sandbox = config_to_dict(cfg)["contexts"]["default"]["sandbox"]
    assert sandbox["computer_use"] is True
    assert sandbox["mingw_bin"] == MINGW


# -- sandbox wiring -----------------------------------------------------------


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.calls: list[tuple] = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def get_vnc_port(self):
        return 5901

    def get_vnc_credentials(self):
        return None

    def get_vnc_quirks(self):
        return frozenset()

    def take_screenshot(self, output_path):
        self.calls.append(("take_screenshot", output_path))

    def send_click(self, x, y, button):
        self.calls.append(("send_click", x, y, button))

    def send_type(self, text):
        self.calls.append(("send_type", text))

    def send_key(self, key_str):
        self.calls.append(("send_key", key_str))

    def send_scroll(self, x, y, direction, amount):
        self.calls.append(("send_scroll", x, y, direction, amount))

    def get_clipboard(self):
        self.calls.append(("get_clipboard",))
        return "clip"

    def set_clipboard(self, text):
        self.calls.append(("set_clipboard", text))


def _make_sandbox(tmp_path, monkeypatch, **config_extra) -> HcsSandbox:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(hcs_mod, "HcsRdpSession", _FakeSession)
    monkeypatch.setattr(
        hcs_mod, "ensure_rdp_helper",
        lambda out_dir, mingw_bin: (
            out_dir / "hcs_rdp_helper.exe", mingw_bin or out_dir,
        ),
    )
    defaults: dict = {
        "backend": "hcs",
        "computer_use": True,
        "base_image": str(tmp_path / "root.vhdx"),
        "mingw_bin": str(tmp_path / "mingw64-bin"),
    }
    defaults.update(config_extra)
    (tmp_path / "mingw64-bin").mkdir(exist_ok=True)
    sb = HcsSandbox(
        "default", SandboxConfig(**defaults), str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
    )
    sb._runtime_id = "11111111-2222-3333-4444-555555555555"
    return sb


def test_session_created_lazily_with_boot_identity(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch)
    assert sb._rdp_session is None
    assert sb.get_vnc_port() == 5901
    session = sb._rdp_session
    assert isinstance(session, _FakeSession)
    assert session.started
    assert session.kwargs["target"] == f"hv:{sb._runtime_id}"
    assert session.kwargs["dll_dir"] == tmp_path / "mingw64-bin"
    assert session.kwargs["exec_fn"] == sb.guest_exec
    assert session.kwargs["helper_exe"] == tmp_path / "state" / "hcs_rdp_helper.exe"
    # Second call reuses the live session.
    assert sb.get_vnc_port() == 5901
    assert sb._rdp_session is session


def test_members_delegate_to_the_session(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb.take_screenshot(tmp_path / "s.png")
    sb.send_click(10, 20, "right")
    sb.send_type("hi")
    sb.send_key("ctrl+a")
    sb.send_scroll(5, 6, "down", 2)
    assert sb.get_clipboard() == "clip"
    sb.set_clipboard("x")
    assert sb._rdp_session.calls == [
        ("take_screenshot", tmp_path / "s.png"),
        ("send_click", 10, 20, "right"),
        ("send_type", "hi"),
        ("send_key", "ctrl+a"),
        ("send_scroll", 5, 6, "down", 2),
        ("get_clipboard",),
        ("set_clipboard", "x"),
    ]
    assert sb.get_vnc_credentials() is None
    assert sb.get_vnc_quirks() == frozenset()
    assert sb.get_screenshots_dir() is None
    assert sb.get_text_input_state_path() is None
    assert sb.get_text_input_active() is False


def test_non_computer_use_context_has_no_session(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch, computer_use=False)
    assert sb.get_vnc_port() is None
    with pytest.raises(NotImplementedError, match="not enabled"):
        sb.take_screenshot(tmp_path / "s.png")
    assert sb._rdp_session is None


def test_not_running_guest_raises_and_vnc_port_degrades(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb._runtime_id = None
    with pytest.raises(RuntimeError, match="not running"):
        sb.send_type("hi")
    assert sb.get_vnc_port() is None


def test_session_starts_without_a_toolchain(tmp_path, monkeypatch):
    # No mingw_bin: helper resolution is handed None and the session still
    # comes up on whatever the prebuilt bundle resolves to.
    sb = _make_sandbox(tmp_path, monkeypatch, mingw_bin=None)
    assert sb.get_vnc_port() == 5901
    assert sb._rdp_session.kwargs["dll_dir"] == tmp_path / "state"


def test_absent_mingw_bin_dir_is_reported(tmp_path, monkeypatch):
    sb = _make_sandbox(
        tmp_path, monkeypatch, mingw_bin=str(tmp_path / "nope"),
    )
    with pytest.raises(RuntimeError, match="not found"):
        sb.take_screenshot(tmp_path / "s.png")


# -- RDP helper resolution ----------------------------------------------------


@pytest.fixture
def helper_env(tmp_path, monkeypatch):
    """Isolate helper resolution: an empty bundle directory, no override, and
    a download that fails unless a test says otherwise."""
    bundle = tmp_path / "bundle"
    monkeypatch.delenv("OPENSHRIMP_HCS_RDP_HELPER", raising=False)
    monkeypatch.setattr(hcs_rdp, "shipped_helper_dir", lambda: bundle)
    monkeypatch.setattr(
        hcs_rdp, "download_shipped_helper",
        lambda: (_ for _ in ()).throw(RuntimeError("no network")),
    )
    return bundle


def _record_build(monkeypatch) -> list[tuple]:
    built: list[tuple] = []

    def fake_build(out_dir, mingw_bin):
        built.append((out_dir, mingw_bin))
        return out_dir / "hcs_rdp_helper.exe"

    monkeypatch.setattr(hcs_rdp, "build_helper_exe", fake_build)
    return built


def test_shipped_helper_wins_over_the_toolchain(tmp_path, monkeypatch, helper_env):
    helper_env.mkdir()
    exe = helper_env / "hcs_rdp_helper.exe"
    exe.write_text("x")
    built = _record_build(monkeypatch)
    mingw = tmp_path / "mingw64-bin"
    assert hcs_rdp.ensure_rdp_helper(tmp_path / "state", mingw) == (
        exe, helper_env,
    )
    assert built == []


def test_env_override_names_the_exe_or_its_directory(
    tmp_path, monkeypatch, helper_env,
):
    staged = tmp_path / "staged"
    staged.mkdir()
    exe = staged / "hcs_rdp_helper.exe"
    exe.write_text("x")
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", str(staged))
    assert hcs_rdp.ensure_rdp_helper(tmp_path / "state", None) == (exe, staged)
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", str(exe))
    assert hcs_rdp.ensure_rdp_helper(tmp_path / "state", None) == (exe, staged)


def test_env_override_pointing_nowhere_is_an_error(
    tmp_path, monkeypatch, helper_env,
):
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="OPENSHRIMP_HCS_RDP_HELPER"):
        hcs_rdp.ensure_rdp_helper(tmp_path / "state", None)


def test_local_build_is_the_fallback(tmp_path, monkeypatch, helper_env):
    built = _record_build(monkeypatch)
    mingw = tmp_path / "mingw64-bin"
    state = tmp_path / "state"
    assert hcs_rdp.ensure_rdp_helper(state, mingw) == (
        state / "hcs_rdp_helper.exe", mingw,
    )
    assert built == [(state, mingw)]


def test_neither_source_names_both_remedies(tmp_path, monkeypatch, helper_env):
    with pytest.raises(RuntimeError) as excinfo:
        hcs_rdp.ensure_rdp_helper(tmp_path / "state", None)
    message = str(excinfo.value)
    assert hcs_rdp.HELPER_ASSET in message
    assert "mingw_bin" in message
    assert "no network" in message


def test_download_unpacks_the_whole_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(hcs_rdp, "shipped_helper_dir", lambda: bundle)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("hcs_rdp_helper.exe", "exe")
        zf.writestr("libfreerdp3.dll", "dll")
    monkeypatch.setattr(
        hcs_rdp.urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(payload.getvalue()),
    )
    exe = hcs_rdp.download_shipped_helper()
    assert exe == bundle / "hcs_rdp_helper.exe"
    # The DLLs are the point: the exe alone would not load.
    assert (bundle / "libfreerdp3.dll").read_text() == "dll"
    assert not list(bundle.glob("*.tmp"))


def test_download_of_an_archive_without_the_exe_is_an_error(
    tmp_path, monkeypatch,
):
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(hcs_rdp, "shipped_helper_dir", lambda: bundle)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("readme.txt", "nothing useful")
    monkeypatch.setattr(
        hcs_rdp.urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(payload.getvalue()),
    )
    with pytest.raises(RuntimeError, match="contains no hcs_rdp_helper.exe"):
        hcs_rdp.download_shipped_helper()


# -- security-key helper ------------------------------------------------------

HELPER = "openshrimp-security-key-vm-helper"


class _FakeGuestExec:
    """Scriptable guest_exec: first matching substring of the joined argv
    decides ``(rc, output)``; unmatched commands succeed with empty output."""

    def __init__(self, results: list[tuple[str, tuple[int, str]]] | None = None):
        self.results = results or []
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, read_timeout=120.0):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for pattern, rc_out in self.results:
            if pattern in joined:
                return rc_out
        return (0, "")

    def joined_calls(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def _security_key_sandbox(tmp_path, monkeypatch, results=None, **config_extra):
    sb = _make_sandbox(tmp_path, monkeypatch, **config_extra)
    fake = _FakeGuestExec(results)
    monkeypatch.setattr(sb, "guest_exec", fake)
    forwards: list[int] = []
    monkeypatch.setattr(sb, "ensure_host_port_forward", forwards.append)
    return sb, fake, forwards


def test_security_key_helper_requires_computer_use(tmp_path, monkeypatch):
    sb, fake, _ = _security_key_sandbox(
        tmp_path, monkeypatch, computer_use=False,
    )
    with pytest.raises(NotImplementedError, match="computer use"):
        sb.start_security_key_helper(
            relay_url="ws://127.0.0.1:8443", session_id="s", token="t",
        )
    assert fake.calls == []


def test_security_key_helper_requires_running_guest(tmp_path, monkeypatch):
    sb, fake, _ = _security_key_sandbox(tmp_path, monkeypatch)
    sb._runtime_id = None
    with pytest.raises(RuntimeError, match="not running"):
        sb.start_security_key_helper(
            relay_url="ws://127.0.0.1:8443", session_id="s", token="t",
        )
    assert fake.calls == []


def test_security_key_helper_without_uhid_is_actionable(tmp_path, monkeypatch):
    sb, fake, _ = _security_key_sandbox(
        tmp_path, monkeypatch, results=[("test -e /dev/uhid", (1, ""))],
    )
    with pytest.raises(RuntimeError, match="OPENSHRIMP_HCS_KERNEL"):
        sb.start_security_key_helper(
            relay_url="ws://127.0.0.1:8443", session_id="s", token="t",
        )
    # Nothing was installed or launched behind the failed probe.
    assert not any("setsid" in c or "install" in c for c in fake.joined_calls())


def test_security_key_helper_launches_when_installed(tmp_path, monkeypatch):
    ensure_calls: list[str] = []
    monkeypatch.setattr(
        hcs_mod, "ensure_security_key_vm_helper",
        lambda machine: ensure_calls.append(machine),
    )
    sb, fake, forwards = _security_key_sandbox(tmp_path, monkeypatch)
    sb.start_security_key_helper(
        relay_url="ws://127.0.0.1:8443", session_id="sess-1", token="tok",
    )
    # The binary was already on the chroot PATH: no download, no install.
    assert ensure_calls == []
    assert not any(c[0] == "install" for c in fake.calls)
    # The loopback relay port was forwarded for the in-guest helper.
    assert forwards == [8443]
    launch = fake.calls[-1]
    assert launch[:2] == ["sh", "-c"]
    assert (
        f"setsid {HELPER} --relay-url ws://127.0.0.1:8443 "
        "--session-id sess-1 --token tok "
        "> /tmp/openshrimp-security-key-helper-sess-1.log 2>&1 < /dev/null &"
    ) == launch[2]


def test_security_key_helper_installs_on_first_use(tmp_path, monkeypatch):
    staged_src = tmp_path / "helper-download"
    staged_src.write_bytes(b"helper-elf")
    monkeypatch.setattr(
        hcs_mod, "ensure_security_key_vm_helper",
        lambda machine: str(staged_src),
    )
    sb, fake, _ = _security_key_sandbox(
        tmp_path, monkeypatch,
        results=[
            (f"command -v {HELPER}", (1, "")),
            ("uname -m", (0, "x86_64\n")),
        ],
    )
    sb.start_security_key_helper(
        relay_url="ws://127.0.0.1:8443", session_id="s", token="t",
    )
    assert (sb._cfg_dir / HELPER).read_bytes() == b"helper-elf"
    assert [
        "install", "-m", "755",
        f"/run/openshrimp/{HELPER}", f"/usr/local/bin/{HELPER}",
    ] in fake.calls
    assert "setsid" in fake.joined_calls()[-1]


def test_security_key_helper_launch_failure_raises(tmp_path, monkeypatch):
    sb, fake, _ = _security_key_sandbox(
        tmp_path, monkeypatch, results=[("setsid", (1, "boom"))],
    )
    with pytest.raises(RuntimeError, match="failed to start: boom"):
        sb.start_security_key_helper(
            relay_url="ws://127.0.0.1:8443", session_id="s", token="t",
        )


def test_security_key_helper_skips_forward_for_external_relay(
    tmp_path, monkeypatch,
):
    sb, fake, forwards = _security_key_sandbox(tmp_path, monkeypatch)
    sb.start_security_key_helper(
        relay_url="wss://relay.example.com:8443", session_id="s", token="t",
    )
    assert forwards == []
    assert "setsid" in fake.joined_calls()[-1]


def test_stop_closes_the_session_before_the_guest_teardown(
    tmp_path, monkeypatch,
):
    sb = _make_sandbox(tmp_path, monkeypatch)
    sb.get_vnc_port()
    session = sb._rdp_session
    events: list[str] = []
    session.stop = lambda: events.append("session-stop")

    fake_win = MagicMock()
    fake_win.HcsError = type("HcsError", (Exception,), {})

    class _Chan:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            events.append("guest-flush")
            return True, ""

    fake_win.ControlChannel = _Chan

    def _op():
        events.append("terminate-phase")
        raise fake_win.HcsError()

    fake_win.HcsOperation = _op
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", fake_win)

    sb.stop()
    assert events[0] == "session-stop"
    assert "guest-flush" in events
    assert sb._rdp_session is None
