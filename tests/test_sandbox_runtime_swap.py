"""What a manager does with a cached sandbox when a second agent backend
(runtime) asks for the same context.

Every backend unions the two runtimes' shares, so the same guest comes back
and the caller's provision pass mounts and installs the newcomer — no
manager tears a guest down to swap the agent running in it.

These tests exercise the manager-level cache decision without touching a
hypervisor, by stubbing each manager's concrete sandbox constructor.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

import open_shrimp.sandbox.hcs as hcs_mod
import open_shrimp.sandbox.libvirt as libvirt_mod
import open_shrimp.sandbox.lima as lima_mod
from open_shrimp import paths
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.manager import (
    HcsSandboxManager,
    LibvirtSandboxManager,
    LimaSandboxManager,
)


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
    """Minimal stand-in for a concrete sandbox: records its runtimes and stop()."""

    def __init__(self, *, runtimes: Any, **_kw: Any) -> None:
        self.runtimes = list(runtimes)
        self.stopped = False

    def add_runtime(self, runtime: Any) -> None:
        if all(r.name != runtime.name for r in self.runtimes):
            self.runtimes.append(runtime)

    @property
    def runtime_names(self) -> set[str]:
        return {r.name for r in self.runtimes}

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


def _lima_manager(monkeypatch) -> LimaSandboxManager:
    monkeypatch.setattr(lima_mod, "LimaSandbox", _FakeSandbox)
    mgr = LimaSandboxManager()
    # create_sandbox refuses without limactl; the fake never runs it.
    mgr._limactl_path = "limactl"
    return mgr


def _hcs_manager(monkeypatch) -> HcsSandboxManager:
    monkeypatch.setattr(hcs_mod, "HcsSandbox", _FakeSandbox)
    return HcsSandboxManager()


def test_same_runtime_reuses_cached_sandbox(monkeypatch):
    mgr = _manager(monkeypatch)
    ctx = _FakeCtx()

    first = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    second = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))

    assert first is second
    assert first.stopped is False


@pytest.mark.parametrize(
    "build_manager", [_manager, _lima_manager, _hcs_manager],
)
def test_a_shared_guest_takes_the_second_runtime_on(monkeypatch, build_manager):
    """Every backend unions the two runtimes' shares, so the second runtime
    joins the live guest instead of rebuilding it."""
    mgr = build_manager(monkeypatch)
    ctx = _FakeCtx()

    claude_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    opencode_sb = mgr.create_sandbox("dev", ctx, runtime=_runtime("opencode"))

    assert opencode_sb is claude_sb
    assert claude_sb.stopped is False
    assert [r.name for r in claude_sb.runtimes] == ["claude", "opencode"]
    # What the guest hosts is the guest's own answer: the manager keeps no
    # second table to fall out of step with the cache.
    assert claude_sb.runtime_names == {"claude", "opencode"}


def test_a_shared_guest_is_not_re_registered(monkeypatch):
    mgr = _manager(monkeypatch)
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
