"""Who is allowed to replace the binary this process is running.

The core polls GitHub and offers Update/Skip over Telegram.  A front end that
ships the core inside its own release installs versions itself, and two
installers for one release means two prompts and a binary swapped out from
under the one that is mid-download.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from open_shrimp.updater import register_update_checker


@dataclass
class _Config:
    auto_update: bool = True


class _JobQueue:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    def run_repeating(self, _callback, interval, first, name):  # noqa: ANN001
        self.jobs.append(name)


class _App:
    def __init__(self, config: _Config) -> None:
        self.bot_data = {"config": config}
        self.job_queue = _JobQueue()


@pytest.fixture
def updatable(monkeypatch):
    """A host the checker would otherwise register on."""
    monkeypatch.setattr("open_shrimp.updater.pyapp_binary_path", lambda: "/bin/openshrimp")
    monkeypatch.setattr(
        "open_shrimp.updater.get_platform_asset_name", lambda: "openshrimp-macos-arm64"
    )
    monkeypatch.delenv("OPENSHRIMP_UPDATES_MANAGED", raising=False)


def test_registers_when_nobody_else_installs_versions(updatable):
    app = _App(_Config())
    register_update_checker(app)
    assert app.job_queue.jobs == ["update_checker"]


def test_a_host_app_that_installs_versions_switches_the_checker_off(updatable, monkeypatch):
    """The macOS app seeds the core from its own bundle and replaces itself
    from a signed feed, so the core polling too would offer a second Update
    button for the same release."""
    monkeypatch.setenv("OPENSHRIMP_UPDATES_MANAGED", "1")

    app = _App(_Config())
    register_update_checker(app)

    assert app.job_queue.jobs == []


def test_being_supervised_is_not_being_updated(updatable, monkeypatch):
    """OPENSHRIMP_SUPERVISED means "restarting it is somebody's job", and the
    Windows tray sets it while shipping no replacement mechanism at all.  A
    checker gated on it would freeze every Windows install at its MSI
    version."""
    monkeypatch.setenv("OPENSHRIMP_SUPERVISED", "1")

    app = _App(_Config())
    register_update_checker(app)

    assert app.job_queue.jobs == ["update_checker"]


def test_config_still_wins(updatable):
    app = _App(_Config(auto_update=False))
    register_update_checker(app)
    assert app.job_queue.jobs == []
