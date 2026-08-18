"""The core's own log file.

A core started by a tray app, a logon task or a service manager has no console
anyone can read, so it owns a rotating log at a path it can name — on every
platform, because a post-mortem nobody can be handed is one that does not
exist.  What is tested here is mostly what it refuses to do: crash the boot
because the directory would not open, and stack a second handler that doubles
every line.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from open_shrimp.main import _attach_file_logging
from open_shrimp.paths import init_paths, log_dir


@pytest.fixture
def clean_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_log_dir_is_instance_scoped():
    init_paths(None)
    default = log_dir()
    init_paths("blue")
    scoped = log_dir()

    # Two cores must never rotate the same file underneath each other.
    assert default != scoped
    assert "blue" in str(scoped)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_every_platform_gets_a_rotating_file(
    clean_root_handlers, monkeypatch, tmp_path, platform: str
):
    """Linux included: the journal is only a log for someone who will run
    journalctl, so the platform must not decide whether a post-mortem exists."""
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path / "Logs")

    _attach_file_logging()

    handlers = [
        h for h in clean_root_handlers.handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(handlers) == 1

    logging.getLogger("open_shrimp").warning("hello from the core")
    handlers[0].flush()
    assert "hello from the core" in (tmp_path / "Logs" / "openshrimp.log").read_text()


def test_the_file_rotates_rather_than_growing_without_a_bound(
    clean_root_handlers, monkeypatch, tmp_path
):
    """An unbounded handler on a core that runs for months fills the disk it
    was installed to help debug."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path / "Logs")

    _attach_file_logging()

    handler = next(
        h for h in clean_root_handlers.handlers if isinstance(h, RotatingFileHandler)
    )
    assert handler.maxBytes > 0
    assert handler.backupCount > 0


def test_attaching_twice_does_not_duplicate(clean_root_handlers, monkeypatch, tmp_path):
    """run_bot_async is re-entered by the macOS app; a second run must not
    stack another handler and double every line."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path / "Logs")

    _attach_file_logging()
    _attach_file_logging()

    assert (
        len([h for h in clean_root_handlers.handlers if isinstance(h, RotatingFileHandler)])
        == 1
    )


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_unwritable_log_dir_does_not_stop_the_core(
    clean_root_handlers, monkeypatch, tmp_path, platform: str
):
    """A log file is a convenience; refusing to boot without one would trade a
    missing post-mortem for a bot that never comes up at all."""
    monkeypatch.setattr("sys.platform", platform)

    def _boom():
        raise OSError("read-only filesystem")

    monkeypatch.setattr("open_shrimp.main.log_dir", _boom)
    before = list(clean_root_handlers.handlers)

    _attach_file_logging()  # must not raise

    assert not any(
        isinstance(h, RotatingFileHandler) for h in clean_root_handlers.handlers
    )
    # The failure cost the file, not the stream: whatever was carrying the log
    # before is still attached and still carrying it.
    assert clean_root_handlers.handlers == before
