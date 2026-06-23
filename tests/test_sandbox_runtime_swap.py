"""When the agent backend (runtime) for a sandboxed context changes, the
cached sandbox is pinned to the old image/launch and must be torn down and
rebuilt — otherwise the new agent is launched inside the previous backend's
container/VM and fails.

These tests exercise the manager-level cache invalidation without touching
Docker by stubbing the concrete ``DockerSandbox`` constructor.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

import open_shrimp.sandbox.docker as docker_mod
from open_shrimp import paths
from open_shrimp.sandbox.manager import DockerSandboxManager


@pytest.fixture(autouse=True)
def _init_paths():
    # The manager reads build-log / state dirs at construction; init_paths
    # only sets module globals (no disk writes).
    paths.init_paths()
    yield


@dataclass
class _FakeContainer:
    docker_in_docker: bool = False
    computer_use: bool = False
    dockerfile: str | None = None
    enabled: bool = True


@dataclass
class _FakeCtx:
    directory: str = "/tmp/openshrimp-fake"
    additional_directories: list[str] = field(default_factory=list)
    container: _FakeContainer = field(default_factory=_FakeContainer)


class _FakeSandbox:
    """Minimal stand-in for DockerSandbox: records its runtime and stop()."""

    def __init__(self, *, runtime: Any, **_kw: Any) -> None:
        self.runtime = runtime
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _runtime(name: str) -> Any:
    return types.SimpleNamespace(name=name)


def _patch_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(docker_mod, "DockerSandbox", _FakeSandbox)


def test_same_runtime_reuses_cached_sandbox(monkeypatch):
    _patch_sandbox(monkeypatch)
    mgr = DockerSandboxManager()
    ctx = _FakeCtx()
    rt = _runtime("claude")

    first = mgr.create_sandbox("dev", ctx, runtime=rt)
    second = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))

    assert first is second
    assert first.stopped is False


def test_runtime_swap_rebuilds_and_stops_old(monkeypatch):
    _patch_sandbox(monkeypatch)
    mgr = DockerSandboxManager()
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
    _patch_sandbox(monkeypatch)
    mgr = DockerSandboxManager()
    ctx = _FakeCtx()

    first = mgr.create_sandbox("dev", ctx, runtime=_runtime("claude"))
    second = mgr.create_sandbox("dev", ctx, runtime=None)

    assert first is second
    assert first.stopped is False
