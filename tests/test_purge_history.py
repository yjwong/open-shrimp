"""Removing a context deletes its kept history only when asked to."""

from __future__ import annotations

from pathlib import Path

import pytest

from open_shrimp import paths
from open_shrimp.sandbox import manager


class _InlineThread:
    def __init__(self, target, daemon: bool) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


class _Manager:
    def __init__(self) -> None:
        self.destroyed: list[str] = []

    def destroy_context(self, name: str) -> None:
        self.destroyed.append(name)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "_data_dir", tmp_path / "data")
    monkeypatch.setattr(manager.threading, "Thread", _InlineThread)
    root = paths.sandbox_state_root()
    for name in ("keep", "purge", "live"):
        (root / name / "claude-home" / "projects").mkdir(parents=True)
    return root


def test_only_removed_contexts_named_for_purge_lose_history(root: Path) -> None:
    mgr = _Manager()
    manager.destroy_contexts_background(
        {"keep", "purge"}, {"libvirt": mgr}, purge_history={"purge", "live"},
    )

    assert sorted(mgr.destroyed) == ["keep", "purge"]
    assert (root / "keep" / "claude-home" / "projects").is_dir()
    assert not (root / "purge").exists()
    # Named for purge but not removed: the context still exists.
    assert (root / "live").is_dir()


def test_removal_keeps_history_by_default(root: Path) -> None:
    manager.destroy_contexts_background({"purge"}, {})

    assert (root / "purge" / "claude-home").is_dir()


@pytest.mark.parametrize("name", ["..", "a/../..", "/etc", "."])
def test_purge_refuses_names_outside_the_root(root: Path, name: str) -> None:
    manager.destroy_contexts_background({name}, {}, purge_history={name})

    assert sorted(p.name for p in root.iterdir()) == ["keep", "live", "purge"]
    assert root.is_dir()
