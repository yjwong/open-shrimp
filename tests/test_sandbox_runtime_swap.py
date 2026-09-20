"""What a manager does with a cached sandbox when a second agent backend
(runtime) asks for the same context.

A backend whose mount plan pins one runtime per guest has to tear the guest
down and rebuild, or the new agent is launched with no home to write to.  A
backend that unions the two runtimes' shares hands the same guest back and
lets the caller's provision pass mount and install the newcomer.

These tests exercise the manager-level cache decision without touching
libvirt by stubbing the concrete ``LibvirtSandbox`` constructor.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

import open_shrimp.sandbox.libvirt as libvirt_mod
from open_shrimp import paths
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.manager import LibvirtSandboxManager


@pytest.fixture(autouse=True)
def _init_paths():
    # The manager reads build-log / state dirs at construction; init_paths
    # only sets module globals (no disk writes).
    paths.init_paths()
    yield


@dataclass
class _FakeCtx:
    directory: str = "/tmp/openshrimp-fake"
    additional_directories: list[str] = field(default_factory=list)
    sandbox: SandboxConfig = field(
        default_factory=lambda: SandboxConfig(backend="libvirt"),
    )


class _FakeSandbox:
    """Minimal stand-in for LibvirtSandbox: records its runtimes and stop()."""

    def __init__(self, *, runtimes: Any, **_kw: Any) -> None:
        self.runtimes = list(runtimes)
        self.stopped = False

    def add_runtime(self, runtime: Any) -> None:
        if all(r.name != runtime.name for r in self.runtimes):
            self.runtimes.append(runtime)

    def stop(self) -> None:
        self.stopped = True


def _runtime(name: str) -> Any:
    return types.SimpleNamespace(name=name)


def _manager(monkeypatch) -> LibvirtSandboxManager:
    monkeypatch.setattr(libvirt_mod, "LibvirtSandbox", _FakeSandbox)
    mgr = LibvirtSandboxManager()
    # create_sandbox refuses without a connection; the fake never uses it.
    mgr._conn = object()
    return mgr


def test_same_runtime_reuses_cached_sandbox(monkeypatch):
    mgr = _manager(monkeypatch)
    ctx = _FakeCtx()

    first = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    second = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))

    assert first is second
    assert first.stopped is False


def test_runtime_swap_rebuilds_and_stops_old(monkeypatch):
    """A manager that has not adopted the union keeps the teardown."""
    mgr = _manager(monkeypatch)
    assert mgr._shares_guest_across_runtimes is False
    ctx = _FakeCtx()

    claude_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    opencode_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("opencode"))

    # A fresh sandbox is built for the new backend...
    assert opencode_sb is not claude_sb
    assert [r.name for r in opencode_sb.runtimes] == ["opencode"]
    # ...and the stale one is torn down.
    assert claude_sb.stopped is True
    # The cache now tracks the new runtime alone.
    assert mgr._sandbox_runtime["dev"] == {"opencode"}
    assert mgr.get_active_sandbox("dev") is opencode_sb


def test_a_shared_guest_takes_the_second_runtime_on(monkeypatch):
    """Once a backend unions the two runtimes' shares, the second runtime
    joins the live guest instead of rebooting it."""
    mgr = _manager(monkeypatch)
    monkeypatch.setattr(
        LibvirtSandboxManager, "_shares_guest_across_runtimes", True,
    )
    ctx = _FakeCtx()

    claude_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    opencode_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("opencode"))

    assert opencode_sb is claude_sb
    assert claude_sb.stopped is False
    assert [r.name for r in claude_sb.runtimes] == ["claude", "opencode"]
    assert mgr._sandbox_runtime["dev"] == {"claude", "opencode"}


def test_a_shared_guest_is_not_re_registered(monkeypatch):
    mgr = _manager(monkeypatch)
    monkeypatch.setattr(
        LibvirtSandboxManager, "_shares_guest_across_runtimes", True,
    )
    ctx = _FakeCtx()

    sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    mgr.create_sandbox("dev", ctx, runtime=_runtime("opencode"))
    mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))

    assert [r.name for r in sb.runtimes] == ["claude", "opencode"]


def test_runtime_none_keeps_cached_sandbox(monkeypatch):
    """A ``None`` runtime must not invalidate an existing sandbox."""
    mgr = _manager(monkeypatch)
    ctx = _FakeCtx()

    first = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    second = mgr.create_sandbox("dev", ctx, runtime=None)

    assert first is second
    assert first.stopped is False
