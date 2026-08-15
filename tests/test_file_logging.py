"""Windows file logging.

A core started by a tray app or a logon task has no console anyone can read,
so it owns its own rotating log.  Everywhere else the log already has a home
and this must stay out of the way.
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


def test_no_file_handler_off_windows(clean_root_handlers, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path)

    _attach_file_logging()

    assert not any(
        isinstance(h, RotatingFileHandler) for h in clean_root_handlers.handlers
    )


def test_windows_gets_a_rotating_file(clean_root_handlers, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path / "Logs")

    _attach_file_logging()

    handlers = [
        h for h in clean_root_handlers.handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(handlers) == 1

    logging.getLogger("open_shrimp").warning("hello from the core")
    handlers[0].flush()
    assert "hello from the core" in (tmp_path / "Logs" / "openshrimp.log").read_text()


def test_attaching_twice_does_not_duplicate(clean_root_handlers, monkeypatch, tmp_path):
    """run_bot_async is re-entered by the macOS app; a second run must not
    stack another handler and double every line."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("open_shrimp.main.log_dir", lambda: tmp_path / "Logs")

    _attach_file_logging()
    _attach_file_logging()

    assert (
        len([h for h in clean_root_handlers.handlers if isinstance(h, RotatingFileHandler)])
        == 1
    )


def test_unwritable_log_dir_does_not_stop_the_core(
    clean_root_handlers, monkeypatch, tmp_path
):
    monkeypatch.setattr("sys.platform", "win32")

    def _boom():
        raise OSError("read-only filesystem")

    monkeypatch.setattr("open_shrimp.main.log_dir", _boom)

    _attach_file_logging()  # must not raise

    assert not any(
        isinstance(h, RotatingFileHandler) for h in clean_root_handlers.handlers
    )
