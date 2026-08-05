"""HCS computer-use wiring: config validation for the hcs backend and the
sandbox's delegation to the host-side RDP session (mocked — no guest)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from open_shrimp.config import (
    SandboxConfig,
    _parse,
    _validate_raw,
    config_to_dict,
)
from open_shrimp.sandbox import hcs as hcs_mod
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


def test_hcs_computer_use_without_mingw_bin_rejected():
    with pytest.raises(ValueError, match="mingw_bin"):
        _validate_raw(_hcs_raw({"computer_use": True}))


def test_mingw_bin_must_be_a_string():
    with pytest.raises(ValueError, match="mingw_bin must be a string"):
        _validate_raw(_hcs_raw({"computer_use": True, "mingw_bin": 3}))


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
        hcs_mod, "ensure_helper_exe",
        lambda out_dir, mingw_bin: out_dir / "hcs_rdp_helper.exe",
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


def test_missing_mingw_bin_config_is_actionable(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch, mingw_bin=None)
    with pytest.raises(RuntimeError, match="mingw_bin"):
        sb.take_screenshot(tmp_path / "s.png")


def test_absent_mingw_bin_dir_is_reported(tmp_path, monkeypatch):
    sb = _make_sandbox(
        tmp_path, monkeypatch, mingw_bin=str(tmp_path / "nope"),
    )
    with pytest.raises(RuntimeError, match="not found"):
        sb.take_screenshot(tmp_path / "s.png")


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
