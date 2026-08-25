"""Only the copy this project downloaded is ever run.

An opencode installed by other means carries a version and an update policy
outside this project's control, and inheriting it is the drift the pin exists
to close — so a ``$PATH`` opencode reads as absent here, and absent is an
answer rather than an error.
"""

from __future__ import annotations

import pytest

import open_shrimp.binaries as binaries
from open_shrimp.backend.opencode import binary as B


@pytest.fixture
def bin_dir(managed_bin_dir, monkeypatch):
    """The suite's empty managed bin directory, with no ``OPENCODE_BIN``."""
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    managed_bin_dir.mkdir(parents=True, exist_ok=True)
    return managed_bin_dir


def _install(bin_dir):
    path = bin_dir / f"opencode{binaries.EXE_SUFFIX}"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_an_absent_binary_answers_none(bin_dir):
    assert B.managed_opencode() is None


def test_the_managed_binary_is_found(bin_dir):
    path = _install(bin_dir)

    assert B.managed_opencode() == str(path)


def test_a_path_opencode_is_not_found(bin_dir, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "opencode").write_text("#!/bin/sh\n")
    (elsewhere / "opencode").chmod(0o755)
    monkeypatch.setenv("PATH", str(elsewhere))

    assert B.managed_opencode() is None


def test_opencode_bin_wins_over_the_managed_binary(bin_dir, monkeypatch, tmp_path):
    _install(bin_dir)
    override = tmp_path / "mine"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OPENCODE_BIN", str(override))

    assert B.managed_opencode() == str(override)


def test_opencode_bin_pointing_at_nothing_falls_through(bin_dir, monkeypatch):
    """A stale override is not a reason to run nothing: the managed copy is
    still there and still the right version."""
    path = _install(bin_dir)
    monkeypatch.setenv("OPENCODE_BIN", str(bin_dir / "gone"))

    assert B.managed_opencode() == str(path)


def test_the_version_stamp_sits_beside_the_binary(bin_dir):
    assert B.version_stamp_path().parent == B.opencode_path().parent
    assert B.version_stamp_path() != B.opencode_path()
