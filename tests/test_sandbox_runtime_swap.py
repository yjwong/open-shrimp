"""When the agent backend (runtime) for a sandboxed context changes, the
cached sandbox is pinned to the old launch and must be torn down and rebuilt —
otherwise the new agent is launched inside the previous backend's guest and
fails.

These tests exercise the manager-level cache invalidation without touching
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
    """Minimal stand-in for LibvirtSandbox: records its runtime and stop()."""

    def __init__(self, *, runtime: Any, **_kw: Any) -> None:
        self.runtime = runtime
        self.stopped = False

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
    mgr = _manager(monkeypatch)
    ctx = _FakeCtx()

    claude_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    opencode_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("opencode"))

    # A fresh sandbox is built for the new backend...
    assert opencode_sb is not claude_sb
    assert opencode_sb.runtime.name == "opencode"
    # ...and the stale one is torn down.
    assert claude_sb.stopped is True
    # The cache now tracks the new runtime.
    assert mgr._sandbox_runtime["dev"] == "opencode"
    assert mgr.get_active_sandbox("dev") is opencode_sb


def test_runtime_none_keeps_cached_sandbox(monkeypatch):
    """A ``None`` runtime must not invalidate an existing sandbox."""
    mgr = _manager(monkeypatch)
    ctx = _FakeCtx()

    first = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    second = mgr.create_sandbox("dev", ctx, runtime=None)

    assert first is second
    assert first.stopped is False
