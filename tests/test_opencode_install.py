"""Fetching the pinned opencode CLI.

Nothing here reaches GitHub: ``stream_to_file`` is redirected at a fixture that
copies a locally built archive, so a checksum mismatch, a hostile member name
and a network failure are all producible without one.
"""

from __future__ import annotations

import hashlib
import io
import multiprocessing
import platform
import tarfile
import zipfile

import pytest

import open_shrimp.binaries as binaries
from open_shrimp.backend.opencode import install as I
from open_shrimp.backend.opencode import release as R

_BODY = b"#!/bin/sh\necho pinned\n"


def _tarball(member: str = "opencode", body: bytes = _BODY) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _zip(member: str = "opencode", body: bytes = _BODY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, body)
    return buf.getvalue()


@pytest.fixture
def host(managed_bin_dir, monkeypatch):
    """A Linux x86_64 host with an empty ``BIN_DIR`` and no ``OPENCODE_BIN``."""
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    return managed_bin_dir


@pytest.fixture
def serve(monkeypatch):
    """Redirect the fetch at a byte string, and record every URL asked for."""
    asked: list[str] = []

    def install(payload: bytes | Exception) -> list[str]:
        def fake(url, dest, *, progress=None, **kwargs):
            asked.append(url)
            if isinstance(payload, Exception):
                raise payload
            dest.write_bytes(payload)
            if progress is not None:
                progress(len(payload), len(payload))

        monkeypatch.setattr(I, "stream_to_file", fake)
        return asked

    return install


def _pin(monkeypatch, payload: bytes, version: str = "9.9.9") -> None:
    """Point the pin at *payload*'s digest, for every slug."""
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(R, "OPENCODE_VERSION", version)
    monkeypatch.setattr(I, "OPENCODE_VERSION", version)
    monkeypatch.setattr(
        R, "OPENCODE_CHECKSUMS", dict.fromkeys(R.OPENCODE_CHECKSUMS, digest)
    )


def test_a_first_fetch_lands_the_binary_and_stamps_its_version(
    host, serve, monkeypatch
):
    payload = _tarball()
    _pin(monkeypatch, payload)
    asked = serve(payload)

    path = I.ensure_opencode_binary()

    assert path == str(host / f"opencode{binaries.EXE_SUFFIX}")
    assert (host / f"opencode{binaries.EXE_SUFFIX}").read_bytes() == _BODY
    assert (host / "opencode.version").read_text() == "9.9.9"
    assert asked == [
        "https://github.com/anomalyco/opencode/releases/download/"
        "v9.9.9/opencode-linux-x64.tar.gz"
    ]


def test_a_second_call_downloads_nothing(host, serve, monkeypatch):
    payload = _tarball()
    _pin(monkeypatch, payload)
    asked = serve(payload)

    I.ensure_opencode_binary()
    I.ensure_opencode_binary()

    assert len(asked) == 1
    assert I.opencode_ready()


def test_a_checksum_mismatch_leaves_no_binary(host, serve, monkeypatch):
    _pin(monkeypatch, b"what the table records")
    serve(_tarball())

    with pytest.raises(RuntimeError, match="does not match the sha256"):
        I.ensure_opencode_binary()

    assert not (host / f"opencode{binaries.EXE_SUFFIX}").exists()
    # And nothing is left behind for the next run to adopt as complete.
    assert list(host.glob("*.download")) == []


def test_a_stale_version_triggers_a_re_download(host, serve, monkeypatch):
    payload = _tarball()
    _pin(monkeypatch, payload, version="1.0.0")
    asked = serve(payload)
    I.ensure_opencode_binary()

    newer = _tarball(body=b"#!/bin/sh\necho newer\n")
    _pin(monkeypatch, newer, version="2.0.0")
    serve(newer)

    assert not I.opencode_ready()
    I.ensure_opencode_binary()

    assert (host / f"opencode{binaries.EXE_SUFFIX}").read_bytes().endswith(b"newer\n")
    assert (host / "opencode.version").read_text() == "2.0.0"
    assert len(asked) == 2


def test_a_failed_re_download_keeps_the_stale_binary(host, serve, monkeypatch, caplog):
    """Refusing to run would turn a GitHub outage into a total outage for a
    user whose opencode works; the next turn retries the upgrade for free."""
    payload = _tarball()
    _pin(monkeypatch, payload, version="1.0.0")
    serve(payload)
    stale = I.ensure_opencode_binary()

    _pin(monkeypatch, payload, version="2.0.0")
    serve(RuntimeError("github is down"))

    with caplog.at_level("WARNING"):
        assert I.ensure_opencode_binary() == stale
    assert "2.0.0" in caplog.text
    assert (host / f"opencode{binaries.EXE_SUFFIX}").read_bytes() == _BODY


def test_a_failed_first_download_raises(host, serve, monkeypatch):
    """There is nothing to fall back to."""
    _pin(monkeypatch, _tarball())
    serve(RuntimeError("github is down"))

    with pytest.raises(RuntimeError, match="github is down"):
        I.ensure_opencode_binary()


def test_a_member_that_walks_out_of_bin_dir_is_refused(host, serve, monkeypatch):
    payload = _zip(member="../../opencode")
    _pin(monkeypatch, payload)
    serve(payload)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")

    with pytest.raises(RuntimeError, match="carries no opencode at its root"):
        I.ensure_opencode_binary()

    assert not (host / f"opencode{binaries.EXE_SUFFIX}").exists()
    assert not (host.parent / "opencode").exists()


def test_a_tarball_member_that_walks_out_is_refused(host, serve, monkeypatch):
    payload = _tarball(member="../opencode")
    _pin(monkeypatch, payload)
    serve(payload)

    with pytest.raises(RuntimeError, match="carries no opencode at its root"):
        I.ensure_opencode_binary()


def test_a_zip_host_unpacks_the_zip(host, serve, monkeypatch):
    payload = _zip()
    _pin(monkeypatch, payload)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    asked = serve(payload)

    I.ensure_opencode_binary()

    assert asked[0].endswith("opencode-darwin-arm64.zip")
    assert (host / f"opencode{binaries.EXE_SUFFIX}").read_bytes() == _BODY


def test_an_unsupported_platform_names_the_machine(host, monkeypatch):
    monkeypatch.setattr(platform, "machine", lambda: "ppc64le")

    with pytest.raises(RuntimeError, match="ppc64le"):
        I.ensure_opencode_binary()


def test_opencode_bin_is_used_as_is(host, monkeypatch, tmp_path):
    """A caller who named a binary is not asking this module to manage one, so
    its version is theirs to worry about and no fetch is attempted."""
    override = tmp_path / "mine"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OPENCODE_BIN", str(override))

    assert I.opencode_ready()
    assert I.ensure_opencode_binary() == str(override)


def _fetch_in_child(bin_dir, payload, counter, downloading, release):
    """One process's whole fetch, for the concurrency test."""
    import open_shrimp.binaries as b
    from open_shrimp.backend.opencode import install as i
    from open_shrimp.backend.opencode import release as r

    b.BIN_DIR = bin_dir
    r.OPENCODE_VERSION = i.OPENCODE_VERSION = "9.9.9"
    digest = hashlib.sha256(payload).hexdigest()
    r.OPENCODE_CHECKSUMS = dict.fromkeys(r.OPENCODE_CHECKSUMS, digest)

    def fake(url, dest, *, progress=None, **kwargs):
        with counter.get_lock():
            counter.value += 1
        # Held open so the winner is still inside the lock while the other
        # child is reaching for it.
        downloading.set()
        release.wait(10)
        dest.write_bytes(payload)

    i.stream_to_file = fake
    i.ensure_opencode_binary()


def test_two_processes_download_once(tmp_path):
    """The colliding fetchers are not in one program — a front end's prefetch
    and the core on a first turn reach one path — so no in-process lock sees
    the pair."""
    # Spawn rather than fork: pytest is multi-threaded by the time this runs,
    # and forking out of it is a documented way to deadlock the child.
    ctx = multiprocessing.get_context("spawn")
    counter = ctx.Value("i", 0)
    downloading, release = ctx.Event(), ctx.Event()
    payload = _tarball()

    children = [
        ctx.Process(
            target=_fetch_in_child,
            args=(tmp_path, payload, counter, downloading, release),
        )
        for _ in range(2)
    ]
    for child in children:
        child.start()
    assert downloading.wait(30), "neither child got as far as a download"
    release.set()
    for child in children:
        child.join(30)

    assert [child.exitcode for child in children] == [0, 0]
    assert counter.value == 1, "the loser woke to find the winner's download"
    assert (tmp_path / f"opencode{binaries.EXE_SUFFIX}").read_bytes() == _BODY


# ── the libvirt installer that donates the host binary ──


def test_the_libvirt_install_counts_the_fetch_into_the_build_log(
    tmp_path, monkeypatch
):
    """The chat's boot message has stopped moving by the time provisioning
    reaches this, so the build log is the only place the transfer shows."""
    from open_shrimp.backend.opencode import libvirt_install as LI

    log = tmp_path / "build.log"
    donated: dict[str, object] = {}

    def fetch(*, progress=None):
        progress(30_000_000, 60_000_000)
        return "/managed/opencode"

    monkeypatch.setattr(LI, "ensure_opencode_binary", fetch)
    monkeypatch.setattr(
        LI, "install_cli_via_ssh",
        lambda name, path, **kw: donated.update(name=name, path=path),
    )

    LI.install_opencode_cli_via_ssh(
        tmp_path / "key", 2222, "openshrimp", log_file=log,
    )

    assert donated == {"name": "opencode", "path": "/managed/opencode"}
    assert "Downloading the opencode CLI: 50% (30 of 60 MB)" in log.read_text()


def test_the_libvirt_install_survives_having_no_build_log(tmp_path, monkeypatch):
    from open_shrimp.backend.opencode import libvirt_install as LI

    seen: list[object] = []

    def fetch(*, progress=None):
        seen.append(progress)
        return "/managed/opencode"

    monkeypatch.setattr(LI, "ensure_opencode_binary", fetch)
    monkeypatch.setattr(LI, "install_cli_via_ssh", lambda *a, **kw: None)

    LI.install_opencode_cli_via_ssh(tmp_path / "key", 2222, "openshrimp")

    assert seen == [None]
