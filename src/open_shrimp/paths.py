"""Centralised, instance-aware path resolution.

Call :func:`init_paths` once at startup (after config is loaded).  All other
modules should import the accessor functions rather than computing paths
themselves.

When ``instance_name`` is *None* (the default single-instance case), every
path is identical to the legacy layout so existing installations keep working
without migration.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from platformdirs import user_data_path

_instance_name: str | None = None
_data_dir: Path | None = None
_build_log_dir: Path | None = None


def init_paths(instance_name: str | None = None) -> None:
    """Initialise the global instance name.  Must be called once at startup."""
    global _instance_name, _data_dir, _build_log_dir
    _instance_name = instance_name

    base = user_data_path("openshrimp")
    _data_dir = base / "instances" / instance_name if instance_name else base

    tmp = Path(tempfile.gettempdir())
    _build_log_dir = (
        tmp / f"openshrimp-{instance_name}-builds" if instance_name
        else tmp / "openshrimp-builds"
    )


# -- Public accessors --------------------------------------------------------

def data_dir() -> Path:
    """Base data directory, scoped by instance name when set."""
    if _data_dir is None:
        raise RuntimeError("paths.init_paths() has not been called yet")
    return _data_dir


def db_path() -> Path:
    """Path to the SQLite database."""
    return data_dir() / "sessions.db"


def build_log_dir() -> Path:
    """Directory for sandbox build logs."""
    if _build_log_dir is None:
        raise RuntimeError("paths.init_paths() has not been called yet")
    return _build_log_dir


def get_instance_name() -> str | None:
    """Return the configured instance name, or *None* for the default instance."""
    if _data_dir is None:
        raise RuntimeError("paths.init_paths() has not been called yet")
    return _instance_name


def instance_prefix() -> str:
    """The instance prefix string (e.g. ``openshrimp-mybot`` or ``openshrimp``)."""
    if _data_dir is None:
        raise RuntimeError("paths.init_paths() has not been called yet")
    if _instance_name:
        return f"openshrimp-{_instance_name}"
    return "openshrimp"
