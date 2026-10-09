"""A Lima guest that Lima reports Running but whose SSH no longer answers.

A stale SSH ControlMaster socket after the host sleeps makes ``limactl shell``
hang until its timeout, so the liveness probe has to read a timeout as "not
responsive", and ``ensure_running`` has to restart such a guest rather than
wait on it forever.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import lima, lima_helpers
from open_shrimp.sandbox.lima import LimaSandbox


def _sandbox(tmp_path: Path) -> LimaSandbox:
    sb = object.__new__(LimaSandbox)
    sb._context_name = "dev"
    sb._config = SandboxConfig(backend="lima")
    sb._guest_os = "linux"
    sb._limactl = "limactl"
    sb._inst_name = "openshrimp-dev"
    return sb


class _Clock:
    """Fake ``time`` whose sleep advances monotonic, so waits cost nothing."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def guest(monkeypatch):
    """A scripted guest: set ``status`` and ``answers`` (a probe → bool fn)."""
    state = {
        "status": "Running",
        "answers": lambda: True,
        "calls": [],
    }
    clock = _Clock()
    monkeypatch.setattr(lima, "time", clock)
    monkeypatch.setattr(
        lima, "limactl_instance_status", lambda limactl, name: state["status"],
    )

    def probe(limactl, name):
        state["calls"].append("probe")
        return state["answers"]()

    def stop(limactl, name, *, force=False):
        state["calls"].append("stop --force" if force else "stop")
        state["status"] = "Stopped"

    def start(limactl, name, *, log_file=None):
        state["calls"].append("start")
        state["status"] = "Running"
        state["answers"] = lambda: True

    monkeypatch.setattr(lima, "limactl_shell_check", probe)
    monkeypatch.setattr(lima, "limactl_stop", stop)
    monkeypatch.setattr(lima, "limactl_start", start)
    return state


def _raise_timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="limactl shell", timeout=10)


def test_a_hung_shell_probe_reads_as_not_running(monkeypatch):
    monkeypatch.setattr(lima_helpers, "_run_limactl", _raise_timeout)

    assert lima_helpers.limactl_shell_check("limactl", "openshrimp-dev") is False


def test_running_is_false_when_the_shell_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lima, "limactl_instance_status", lambda limactl, name: "Running",
    )
    monkeypatch.setattr(lima_helpers, "_run_limactl", _raise_timeout)

    assert _sandbox(tmp_path).running() is False


def test_a_responsive_running_guest_is_left_alone(tmp_path, guest):
    _sandbox(tmp_path).ensure_running()

    assert guest["calls"] == ["probe"]


def test_a_running_guest_that_never_answers_is_force_restarted(tmp_path, guest):
    guest["answers"] = lambda: False

    _sandbox(tmp_path).ensure_running()

    assert "stop --force" in guest["calls"]
    assert guest["calls"][-2:] == ["start", "probe"]


def test_a_running_guest_that_answers_late_is_not_restarted(tmp_path, guest):
    """Lima reports Running before a concurrent boot's SSH is up."""
    replies = iter([False, False, True])
    guest["answers"] = lambda: next(replies)

    _sandbox(tmp_path).ensure_running()

    assert guest["calls"] == ["probe", "probe", "probe"]


def test_a_guest_that_never_boots_raises(tmp_path, guest, monkeypatch):
    guest["status"] = "Stopped"

    def start(limactl, name, *, log_file=None):
        guest["calls"].append("start")
        guest["status"] = "Running"
        guest["answers"] = lambda: False

    monkeypatch.setattr(lima, "limactl_start", start)

    with pytest.raises(RuntimeError, match="not responsive"):
        _sandbox(tmp_path).ensure_running()
    assert "stop --force" not in guest["calls"]
