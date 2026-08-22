"""Fetching the released guest artifacts an HCS sandbox boots from.

The value here is not the download itself but what surrounds it: nothing is
put in place before its checksum matches, a partial transfer never survives as
something that looks staged, and a cached artifact is never re-fetched.  All of
it runs against a stubbed ``urlopen``, so no test touches the network.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
from pathlib import Path

import pytest
import zstandard

from open_shrimp.sandbox import hcs_assets as A


class _Body(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` yields.

    Carries headers because the fetch reads ``Content-Length`` off them to
    give progress a denominator; *length* of ``None`` is the server that
    declined to say.
    """

    def __init__(self, data: bytes, *, length: int | None = -1):
        super().__init__(data)
        size = len(data) if length == -1 else length
        self.headers = {} if size is None else {"Content-Length": str(size)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _serve(monkeypatch, bodies: dict[str, bytes]):
    """Answer ``urlopen`` from *bodies*, keyed by asset name.

    Returns the list every requested URL is appended to, so a test can assert
    what was fetched — and, more usefully, what was not.
    """
    requested: list[str] = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        requested.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in bodies:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Body(bodies[name])

    monkeypatch.setattr(A.urllib.request, "urlopen", fake_urlopen)
    return requested


def _checksum_line(name: str, payload: bytes) -> bytes:
    return f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()


def _assets(name: str, payload: bytes) -> dict[str, bytes]:
    return {name: payload, f"{name}.sha256": _checksum_line(name, payload)}


# -- plain assets -------------------------------------------------------------


def test_downloads_and_verifies(tmp_path, monkeypatch):
    payload = b"initramfs bytes"
    _serve(monkeypatch, _assets("thing.img", payload))
    dest = tmp_path / "sub" / "initrd.img"

    got = A.ensure_asset("thing.img", dest, description="the thing")

    assert got == dest
    assert dest.read_bytes() == payload


def test_cached_asset_is_not_refetched(tmp_path, monkeypatch):
    dest = tmp_path / "initrd.img"
    dest.write_bytes(b"already here")
    requested = _serve(monkeypatch, _assets("thing.img", b"other"))

    A.ensure_asset("thing.img", dest, description="the thing")

    assert requested == []
    assert dest.read_bytes() == b"already here"


def test_checksum_mismatch_leaves_nothing_behind(tmp_path, monkeypatch):
    """A corrupt download must not land: an artifact that looks staged would
    be adopted on the next boot and fail as a broken guest instead."""
    bodies = _assets("thing.img", b"the real bytes")
    bodies["thing.img"] = b"tampered"
    _serve(monkeypatch, bodies)
    dest = tmp_path / "initrd.img"

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        A.ensure_asset("thing.img", dest, description="the thing")

    assert not dest.exists()
    assert _leftovers(tmp_path) == []


def test_missing_checksum_is_an_error(tmp_path, monkeypatch):
    _serve(monkeypatch, {"thing.img": b"payload"})  # no .sha256 published
    dest = tmp_path / "initrd.img"

    with pytest.raises(RuntimeError, match="could not fetch its checksum"):
        A.ensure_asset("thing.img", dest, description="the thing")

    assert not dest.exists()


def test_failed_download_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, {})
    dest = tmp_path / "initrd.img"

    with pytest.raises(RuntimeError, match="Failed to download"):
        A.ensure_asset("thing.img", dest, description="the thing")

    assert not dest.exists()
    assert _leftovers(tmp_path) == []


def _leftovers(directory: Path) -> list[Path]:
    """What a fetch left in *directory*, excluding the lock files it keeps.

    A lock file is created once and never removed: unlinking one would let
    two processes hold locks on two different inodes reached by one name,
    which is the race it exists to close.  Everything else here is either the
    artifact or a staging file that should have been swept.
    """
    return sorted(p for p in directory.iterdir() if p.suffix != ".lock")


# -- compressed assets --------------------------------------------------------


def test_zst_asset_is_unpacked(tmp_path, monkeypatch):
    """The checksum covers the compressed asset as published, and *dest* holds
    the unpacked image — the form the sandbox boots."""
    raw = b"vhdx contents" * 1000
    compressed = zstandard.ZstdCompressor().compress(raw)
    _serve(monkeypatch, _assets("rootfs.vhdx.zst", compressed))
    dest = tmp_path / "base-rootfs.vhdx"

    A.ensure_asset("rootfs.vhdx.zst", dest, description="the rootfs")

    assert dest.read_bytes() == raw
    assert _leftovers(tmp_path) == [dest]


def test_corrupt_zst_leaves_nothing_behind(tmp_path, monkeypatch):
    """A payload whose checksum matches but which is not a zstd frame — a
    mispublished asset — must not leave a truncated image at *dest*."""
    payload = b"not a zstd frame at all"
    _serve(monkeypatch, _assets("rootfs.vhdx.zst", payload))
    dest = tmp_path / "base-rootfs.vhdx"

    with pytest.raises(RuntimeError, match="not a valid zstd archive"):
        A.ensure_asset("rootfs.vhdx.zst", dest, description="the rootfs")

    assert not dest.exists()
    assert _leftovers(tmp_path) == []


# -- progress reporting -------------------------------------------------------


def test_progress_goes_to_the_supplied_sink(tmp_path, monkeypatch):
    _serve(monkeypatch, _assets("thing.img", b"payload"))
    lines: list[str] = []

    A.ensure_asset(
        "thing.img", tmp_path / "initrd.img",
        description="the control initramfs", log=lines.append,
    )

    assert any("the control initramfs" in line for line in lines)


def test_bytes_are_counted_against_the_published_length(tmp_path, monkeypatch):
    payload = b"x" * (A._CHUNK * 2 + 11)
    _serve(monkeypatch, _assets("rootfs.vhdx.zst", payload))
    seen: list[tuple[int, int | None]] = []

    with pytest.raises(RuntimeError, match="not a valid zstd archive"):
        # The transfer is what is under test; it has already happened by the
        # time the unpack complains about what came down the wire.
        A.ensure_asset(
            "rootfs.vhdx.zst", tmp_path / "base-rootfs.vhdx",
            description="the rootfs",
            progress=lambda done, total: seen.append((done, total)),
        )

    assert seen == [
        (A._CHUNK, len(payload)),
        (A._CHUNK * 2, len(payload)),
        (len(payload), len(payload)),
    ]


def test_a_server_that_sends_no_length_leaves_the_total_unknown(
    tmp_path, monkeypatch,
):
    """Unknown must reach the caller as ``None``: a zero would render as a
    progress bar frozen at nothing rather than as an indeterminate one."""
    payload = b"payload"
    bodies = _assets("thing.img", payload)

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        return _Body(bodies[url.rsplit("/", 1)[-1]], length=None)

    monkeypatch.setattr(A.urllib.request, "urlopen", fake_urlopen)
    seen: list[tuple[int, int | None]] = []

    A.ensure_asset(
        "thing.img", tmp_path / "initrd.img", description="the thing",
        progress=lambda done, total: seen.append((done, total)),
    )

    assert seen == [(len(payload), None)]


# -- the raw download primitive ----------------------------------------------


def test_download_release_asset_is_atomic(tmp_path, monkeypatch):
    """The RDP helper bundle rides this path; it publishes no checksum, so the
    all-or-nothing replace is the only guarantee it has."""
    _serve(monkeypatch, {})
    dest = tmp_path / "bundle.zip"

    with pytest.raises(RuntimeError, match="Failed to download"):
        A.download_release_asset("bundle.zip", dest)

    assert not dest.exists()
    assert _leftovers(tmp_path) == []


def test_release_asset_url_points_at_the_latest_release():
    url = A.release_asset_url("openshrimp-hcs-initrd.img")
    assert url.endswith("/releases/latest/download/openshrimp-hcs-initrd.img")
