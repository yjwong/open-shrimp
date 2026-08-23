"""Pre-fetching a sandbox backend's shared assets, and the progress it reports.

The download itself is the least interesting part.  What matters is the shape
of what a front end reads: an unknown size is an absent ``total`` rather than
a zero one, an asset already on disk costs nothing, the byte counter stays
exact while the reporting stays coarse, and a disk that cannot hold the
result says so before the first byte moves.

The exception is the lock.  A cached asset is shared by every context and
every process on the host, so the fetchers that collide on one are separate
programs, and the tests at the end of this file spawn real interpreters to
prove that only one of them downloads.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import http.server
import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from open_shrimp.main import _run_sandbox_prefetch
from open_shrimp.sandbox import prefetch as P


def _asset(
    name: str,
    *,
    directory: Path,
    chunks: list[tuple[int, int | None]] | None = None,
    present: bool = False,
    needs_bytes: int = 0,
    fetched: list[str] | None = None,
) -> P.SharedAsset:
    """A stand-in asset whose fetch replays *chunks* through the callback."""

    def fetch(progress: P.ProgressFn) -> None:
        if fetched is not None:
            fetched.append(name)
        for done, total in chunks or []:
            progress(done, total)

    return P.SharedAsset(
        name=name,
        directory=directory,
        needs_bytes=needs_bytes,
        present=lambda: present,
        fetch=fetch,
    )


def _events(assets: list[P.SharedAsset], monkeypatch) -> list[dict]:
    monkeypatch.setattr(P, "shared_assets", lambda backend: assets)
    # The throttle has its own tests; here it would only make the shape
    # assertions depend on how fast the machine ran them.
    monkeypatch.setattr(P, "throttled", lambda report, clock=None: report)
    seen: list[dict] = []
    P.prefetch("whatever", emit=seen.append)
    return seen


# -- the event shape ----------------------------------------------------------


def test_a_known_size_is_reported_as_a_denominator(tmp_path, monkeypatch):
    events = _events(
        [_asset("rootfs.vhdx", directory=tmp_path, chunks=[(10, 100), (100, 100)])],
        monkeypatch,
    )

    assert events[:2] == [
        {"asset": "rootfs.vhdx", "done": 10, "total": 100},
        {"asset": "rootfs.vhdx", "done": 100, "total": 100},
    ]


def test_an_unknown_size_omits_total_rather_than_sending_zero(tmp_path, monkeypatch):
    """A zero denominator renders as a bar that never moves, which is a worse
    lie than a bar admitting it does not know how far along it is."""
    events = _events(
        [_asset("rootfs.vhdx", directory=tmp_path, chunks=[(10, None)])],
        monkeypatch,
    )

    assert events[0] == {"asset": "rootfs.vhdx", "done": 10}
    assert "total" not in events[0]


def test_each_asset_is_followed_by_its_ready_and_the_run_by_finished(
    tmp_path, monkeypatch,
):
    events = _events(
        [
            _asset("one", directory=tmp_path, chunks=[(1, 2)]),
            _asset("two", directory=tmp_path, chunks=[(1, 2)]),
        ],
        monkeypatch,
    )

    assert events == [
        {"asset": "one", "done": 1, "total": 2},
        {"asset": "one", "state": "ready"},
        {"asset": "two", "done": 1, "total": 2},
        {"asset": "two", "state": "ready"},
        {"state": "finished"},
    ]


def test_an_asset_already_on_disk_is_ready_without_being_fetched(
    tmp_path, monkeypatch,
):
    fetched: list[str] = []
    events = _events(
        [
            _asset(
                "cached", directory=tmp_path, present=True,
                chunks=[(1, 2)], fetched=fetched,
            ),
        ],
        monkeypatch,
    )

    assert fetched == []
    assert events == [{"asset": "cached", "state": "ready"}, {"state": "finished"}]


def test_an_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="Unknown sandbox backend"):
        P.shared_assets("qubes")


# -- the throttle -------------------------------------------------------------


def test_progress_arriving_faster_than_the_interval_is_dropped():
    """A 64 KB chunk on a fast link fires thousands of times a second; a
    consumer parsing all of them reads for longer than the transfer moves."""
    now = 0.0
    seen: list[tuple[int, int | None]] = []
    report = P.throttled(lambda done, total: seen.append((done, total)), clock=lambda: now)

    for i in range(1000):
        now = i * 0.001  # a millisecond apart — one full second of chunks
        report(i, 1000)

    # Four a second, not a thousand.
    assert len(seen) <= 5
    assert seen[0] == (0, 1000)


def test_the_throttle_reports_the_exact_count_not_a_smoothed_one():
    """Dropping reports must not drop bytes: whatever does get through is the
    true running total at that moment."""
    now = 0.0
    seen: list[tuple[int, int | None]] = []
    report = P.throttled(lambda done, total: seen.append((done, total)), clock=lambda: now)

    report(100, 900)
    now = 1.0
    report(700, 900)

    assert seen == [(100, 900), (700, 900)]


# -- Content-Length parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2147483648", 2147483648),
        (" 512 ", 512),
        ("0", 0),
        (None, None),
        ("", None),
        ("chunked", None),
        ("-1", None),
    ],
)
def test_content_length_reads_a_size_or_admits_it_does_not_know(raw, expected):
    assert P.content_length(raw) == expected


# -- the free-space check -----------------------------------------------------


def test_a_disk_too_small_is_refused_before_anything_is_fetched(
    tmp_path, monkeypatch,
):
    """Gigabytes onto a full disk fail minutes later and blame whichever write
    happened to be unlucky."""
    fetched: list[str] = []
    asset = _asset(
        "rootfs.vhdx", directory=tmp_path / "not" / "yet" / "there",
        needs_bytes=1 << 50, fetched=fetched,
    )
    monkeypatch.setattr(P, "shared_assets", lambda backend: [asset])

    with pytest.raises(RuntimeError, match="Not enough free space"):
        P.prefetch("whatever", emit=lambda event: None)

    assert fetched == []


def test_assets_sharing_a_filesystem_must_fit_on_it_together(tmp_path):
    import shutil

    free = shutil.disk_usage(tmp_path).free
    two_thirds = int(free * 0.67)

    assert P.space_shortfall([_asset("a", directory=tmp_path, needs_bytes=two_thirds)]) is None
    assert P.space_shortfall([
        _asset("a", directory=tmp_path, needs_bytes=two_thirds),
        _asset("b", directory=tmp_path, needs_bytes=two_thirds),
    ]) is not None


def test_one_disk_is_one_budget_even_from_two_directories(tmp_path):
    """Two assets under one data directory resolve to different existing
    ancestors as soon as one destination has been created; budgeting per path
    instead of per device would let a pair that only fits one at a time both
    pass, and the disk would fill partway through the second."""
    import shutil

    made = tmp_path / "bin"
    made.mkdir()
    two_thirds = int(shutil.disk_usage(tmp_path).free * 0.67)

    assert P.space_shortfall([
        _asset("a", directory=made, needs_bytes=two_thirds),
        _asset("b", directory=tmp_path / "images", needs_bytes=two_thirds),
    ]) is not None


def test_space_for_an_asset_already_present_is_not_demanded(tmp_path, monkeypatch):
    """A host that has everything already must not be told it lacks the room
    to fetch what it is not going to fetch."""
    monkeypatch.setattr(
        P, "shared_assets",
        lambda backend: [
            _asset("huge", directory=tmp_path, needs_bytes=1 << 50, present=True),
        ],
    )
    seen: list[dict] = []

    P.prefetch("whatever", emit=seen.append)

    assert seen == [{"asset": "huge", "state": "ready"}, {"state": "finished"}]


# -- the CLI surface ----------------------------------------------------------


def test_the_command_writes_one_json_object_per_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        P, "shared_assets",
        lambda backend: [
            _asset("rootfs.vhdx", directory=tmp_path, chunks=[(5, None), (10, None)]),
        ],
    )
    monkeypatch.setattr(P, "throttled", lambda report, clock=None: report)

    assert _run_sandbox_prefetch(backend="libvirt", json_output=True) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"asset": "rootfs.vhdx", "done": 5},
        {"asset": "rootfs.vhdx", "done": 10},
        {"asset": "rootfs.vhdx", "state": "ready"},
        {"state": "finished"},
    ]


def test_a_failed_fetch_exits_non_zero_with_the_reason_on_stderr(
    tmp_path, monkeypatch, capsys,
):
    """The contract ``sandboxes --json`` already follows: a core that would not
    answer is nothing, not a half-parsed stream."""

    def explode(progress):
        raise RuntimeError("the release has no such asset")

    monkeypatch.setattr(
        P, "shared_assets",
        lambda backend: [
            P.SharedAsset(
                name="rootfs.vhdx", directory=tmp_path, needs_bytes=0,
                present=lambda: False, fetch=explode,
            ),
        ],
    )

    assert _run_sandbox_prefetch(backend="hcs", json_output=True) != 0

    captured = capsys.readouterr()
    assert "the release has no such asset" in captured.err
    assert "finished" not in captured.out


def test_no_backend_named_falls_back_to_the_one_this_host_is_offered(
    monkeypatch, capsys,
):
    """A front end that asked ``sandboxes`` which backend to write must not
    have to decide a second time here."""
    import open_shrimp.doctor as doctor

    asked: list[str] = []
    monkeypatch.setattr(
        doctor, "blessed_offer",
        lambda config: type("Offer", (), {"backend": "libvirt"})(),
    )
    monkeypatch.setattr(
        P, "shared_assets",
        lambda backend: asked.append(backend) or [],
    )

    assert _run_sandbox_prefetch(backend=None, json_output=True) == 0
    assert asked == ["libvirt"]
    assert json.loads(capsys.readouterr().out.strip()) == {"state": "finished"}


def test_a_host_with_no_sandbox_at_all_says_so(monkeypatch, capsys):
    import open_shrimp.doctor as doctor

    monkeypatch.setattr(doctor, "blessed_offer", lambda config: None)

    assert _run_sandbox_prefetch(backend=None, json_output=True) != 0
    assert "nothing to prefetch" in capsys.readouterr().err


# -- the asset a wizard's own config boots ------------------------------------


def _wizard_sandbox(backend: str):
    """The sandbox block a setup UI writes, parsed the way the core parses it.

    Through both real functions rather than a hand-written dict: what a wizard
    enables is exactly the question this file is pinning, so a literal here
    would pin the assumption instead of the fact.
    """
    from open_shrimp.config import _parse_sandbox_config, build_context_dict

    context = build_context_dict("/tmp", "a project", sandbox=backend)
    return _parse_sandbox_config(context["sandbox"])


def _recorded_downloads(monkeypatch, tmp_path) -> list[tuple[str, Path]]:
    """Every ``(asset, destination)`` the HCS fetchers would have downloaded.

    ``download_release_asset`` is stopped as well as ``ensure_asset``: the RDP
    helper goes through that one, and leaving it open pulls the real 50 MB
    bundle off the network on every run.  Its stand-in writes a zip holding the
    exe, because the caller unpacks what it is handed.
    """
    from open_shrimp.sandbox import hcs_assets, hcs_rdp

    seen: list[tuple[str, Path]] = []

    def record(asset, dest, *, description, log=None, progress=None):
        seen.append((asset, Path(dest)))
        return Path(dest)

    def record_raw(asset, dest, *, progress=None):
        seen.append((asset, Path(dest)))
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr(hcs_rdp.HELPER_EXE_NAME, b"MZ")

    monkeypatch.setattr(hcs_assets, "ensure_asset", record)
    monkeypatch.setattr(hcs_assets, "download_release_asset", record_raw)
    # The bundle lands per-user, so without this the suite writes into the
    # data directory of whoever is running it.
    monkeypatch.setattr(hcs_rdp, "shipped_helper_dir", lambda: tmp_path / "helper")
    return seen


def test_the_hcs_prefetch_fetches_the_rootfs_that_config_will_boot(
    monkeypatch, tmp_path,
):
    """Prefetching an image the first turn does not read is worse than not
    prefetching at all: it spends the download twice, once on a file nothing
    opens and once on the one it needed.

    So the image is not named here.  It is asked of the backend, for the
    sandbox block a setup UI actually writes — a claim in the prefetch about
    what a wizard enables is the kind that cannot fail on its own.
    """
    from open_shrimp.sandbox import hcs

    wanted = _wizard_sandbox("hcs")
    asset, cache, _ = hcs.managed_rootfs_asset(computer_use=wanted.computer_use)

    downloads = _recorded_downloads(monkeypatch, tmp_path)
    for shared in P.shared_assets("hcs"):
        shared.fetch(lambda done, total: None)

    assert (asset, cache) in downloads


def test_the_hcs_prefetch_stages_the_rdp_helper(monkeypatch, tmp_path):
    """The helper is resolved on the first computer-use call rather than on
    boot, so left out of the prefetch it downloads in the middle of a turn.
    Its precondition is the desktop rootfs's, which the same wizard block
    enables.
    """
    from open_shrimp.sandbox import hcs_rdp

    assert _wizard_sandbox("hcs").computer_use

    downloads = _recorded_downloads(monkeypatch, tmp_path)
    for shared in P.shared_assets("hcs"):
        shared.fetch(lambda done, total: None)

    assert hcs_rdp.HELPER_ASSET in [asset for asset, _ in downloads]
    assert (tmp_path / "helper" / hcs_rdp.HELPER_EXE_NAME).is_file()


def test_a_staged_rdp_helper_is_not_fetched_again(monkeypatch, tmp_path):
    """``present`` is what spares a machine the 50 MB on every run after the
    first, so it has to answer for the helper as it does for the images."""
    from open_shrimp.sandbox import hcs_rdp

    _recorded_downloads(monkeypatch, tmp_path)
    helper = tmp_path / "helper" / hcs_rdp.HELPER_EXE_NAME
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"MZ")

    staged = [a for a in P.shared_assets("hcs") if a.name == hcs_rdp.HELPER_ASSET]
    assert [a.name for a in staged] == [hcs_rdp.HELPER_ASSET]
    assert staged[0].present()


# -- the fetchers -------------------------------------------------------------


def test_lima_counts_bytes_against_the_tarball_length(tmp_path, monkeypatch):
    """The macOS backend's binary is a tarball fetched over httpx; the count is
    of what crosses the network, not of what comes out of the archive."""
    from open_shrimp.sandbox import lima_helpers

    payload = b"x" * (65536 * 3 + 7)
    seen: list[tuple[int, int | None]] = []

    class _Stream:
        headers = {"content-length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            for start in range(0, len(payload), chunk_size):
                yield payload[start:start + chunk_size]

    class _Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url):
            return _Stream()

    monkeypatch.setattr(lima_helpers, "bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(
        lima_helpers, "_DOWNLOAD_MAP", {("Linux", "x86_64"): "lima.tar.gz"},
    )
    monkeypatch.setattr(lima_helpers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(lima_helpers.platform, "machine", lambda: "x86_64")
    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)

    with pytest.raises(Exception):
        # The tarball is not a tarball; the transfer is what is under test and
        # it has already happened by the time the extraction complains.
        lima_helpers._download_lima_sync(progress=lambda d, t: seen.append((d, t)))

    assert seen[0] == (65536, len(payload))
    assert seen[-1] == (len(payload), len(payload))


def test_libvirt_streams_the_cloud_image_and_lands_it_atomically(
    tmp_path, monkeypatch,
):
    from open_shrimp.sandbox import libvirt_helpers

    payload = b"qcow2 bytes" * 10000
    seen: list[tuple[int, int | None]] = []

    class _Body(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Body(payload),
    )
    dest = tmp_path / "images" / "cloud.img"

    libvirt_helpers._stream_download(
        "https://example.invalid/cloud.img", dest,
        progress=lambda d, t: seen.append((d, t)),
    )

    assert dest.read_bytes() == payload
    # Nothing beside it: a ".download" left behind would be mistaken for the
    # image on a later run.
    assert list(dest.parent.iterdir()) == [dest]
    assert seen[-1] == (len(payload), len(payload))


def test_a_body_that_stops_short_of_its_declared_length_is_a_failure(
    tmp_path, monkeypatch,
):
    """``http.client`` ends its read loop on a premature close rather than
    raising, so without this check a dropped connection installs a truncated
    image that every later run adopts as complete."""

    class _Truncated(io.BytesIO):
        headers = {"Content-Length": "1000000"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Truncated(b"x" * 40000),
    )
    dest = tmp_path / "cloud.img"

    with pytest.raises(RuntimeError, match="40000 bytes of the 1000000"):
        P.stream_to_file("https://example.invalid/cloud.img", dest)


def test_a_libvirt_download_that_dies_leaves_no_half_image(tmp_path, monkeypatch):
    from open_shrimp.sandbox import libvirt_helpers

    class _Broken(io.BytesIO):
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            raise OSError("connection reset")

    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Broken(b""),
    )
    dest = tmp_path / "images" / "cloud.img"

    with pytest.raises(RuntimeError, match="Failed to download"):
        libvirt_helpers._stream_download("https://example.invalid/cloud.img", dest)

    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


# -- the libvirt base image's checksum ----------------------------------------
#
# A lock makes two fetchers collide rarely rather than routinely; it does not
# make a corrupt base image detectable.  This backend adopts whatever is at
# the cached path forever, and ``create_overlay`` asserts the format rather
# than probing it, so the first symptom of a bad image is a guest that will
# not boot.


def _listing(*entries: tuple[bytes, str]) -> bytes:
    """A ``sha256sum`` listing in the form Ubuntu publishes it."""
    return b"".join(
        f"{hashlib.sha256(body).hexdigest()} *{name}\n".encode()
        for body, name in entries
    )


def _canned(responses: dict[str, bytes]):
    """A ``urlopen`` answering each URL with its canned body."""

    class _Body(io.BytesIO):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url not in responses:
            raise urllib.error.URLError("no such thing")
        return _Body(responses[url])

    return urlopen


_SUMS = "https://images.example/SHA256SUMS"


def test_the_published_digest_is_found_among_the_other_artifacts(
    tmp_path, monkeypatch,
):
    """One listing covers every artifact in the directory, so the line for
    this file has to be picked out of dozens for files nobody asked for."""
    from open_shrimp.sandbox import libvirt_helpers

    payload = b"qcow2 bytes" * 1000
    monkeypatch.setattr(urllib.request, "urlopen", _canned({
        _SUMS: _listing(
            (b"a squashfs", "noble-server-cloudimg-amd64.squashfs"),
            (payload, "noble-server-cloudimg-amd64.img"),
            (b"a manifest", "noble-server-cloudimg-arm64.manifest"),
        ),
    }))
    image = tmp_path / "cloud.img"
    image.write_bytes(payload)

    libvirt_helpers._verify_sha256(
        image, sums_url=_SUMS, filename="noble-server-cloudimg-amd64.img",
    )


def test_an_image_that_fails_its_digest_is_a_failed_download(
    tmp_path, monkeypatch,
):
    from open_shrimp.sandbox import libvirt_helpers

    monkeypatch.setattr(urllib.request, "urlopen", _canned({
        _SUMS: _listing((b"the real image", "cloud.img")),
    }))
    image = tmp_path / "cloud.img"
    image.write_bytes(b"two fetchers interleaved")

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        libvirt_helpers._verify_sha256(
            image, sums_url=_SUMS, filename="cloud.img",
        )


def test_a_listing_without_our_line_is_an_error_not_an_unverified_install(
    tmp_path, monkeypatch,
):
    """The artifact and its digest are published together, so a listing that
    does not name it means the file fetched is not the one expected."""
    from open_shrimp.sandbox import libvirt_helpers

    monkeypatch.setattr(urllib.request, "urlopen", _canned({
        _SUMS: _listing((b"something else", "some-other.img")),
    }))
    image = tmp_path / "cloud.img"
    image.write_bytes(b"whatever")

    with pytest.raises(RuntimeError, match="publishes no checksum"):
        libvirt_helpers._verify_sha256(
            image, sums_url=_SUMS, filename="cloud.img",
        )


def test_a_listing_that_cannot_be_reached_is_an_error(tmp_path, monkeypatch):
    from open_shrimp.sandbox import libvirt_helpers

    monkeypatch.setattr(urllib.request, "urlopen", _canned({}))
    image = tmp_path / "cloud.img"
    image.write_bytes(b"whatever")

    with pytest.raises(RuntimeError, match="could not fetch its checksum"):
        libvirt_helpers._verify_sha256(
            image, sums_url=_SUMS, filename="cloud.img",
        )


def test_an_image_failing_verification_never_reaches_the_cached_path(
    tmp_path, monkeypatch,
):
    """Verification runs against the staging file, so a bad body is a failed
    download rather than a corruption every later run adopts."""
    from open_shrimp.sandbox import libvirt_helpers

    url = "https://images.example/cloud.img"
    monkeypatch.setattr(urllib.request, "urlopen", _canned({
        url: b"the bytes that arrived",
        _SUMS: _listing((b"the bytes that were published", "cloud.img")),
    }))
    dest = tmp_path / "images" / "cloud.img"

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        libvirt_helpers._stream_download(
            url, dest,
            verify=lambda staged: libvirt_helpers._verify_sha256(
                staged, sums_url=_SUMS, filename="cloud.img",
            ),
        )

    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


# -- the progress a waiting user reads ----------------------------------------


@pytest.mark.parametrize(
    "done,total,expected",
    [
        (2_100_000_000, 6_000_000_000, "35% (2.1 of 6.0 GB)"),
        (0, 6_000_000_000, "0% (0.0 of 6.0 GB)"),
        (6_000_000_000, 6_000_000_000, "100% (6.0 of 6.0 GB)"),
        # Both figures take the unit the larger one calls for, so nobody is
        # asked to compare megabytes against gigabytes inside one bracket.
        (18_000_000, 512_000_000, "3% (18 of 512 MB)"),
    ],
)
def test_a_declared_length_is_reported_as_a_percentage(done, total, expected):
    assert P.describe_bytes(done, total) == expected


def test_an_undeclared_length_drops_the_percentage_rather_than_inventing_one():
    """A bar pinned at 0% for ten minutes is a worse lie than a count of bytes
    with nothing to measure it against."""
    assert P.describe_bytes(2_100_000_000, None) == "2.1 GB downloaded"
    assert P.describe_bytes(18_000_000, 0) == "18 MB downloaded"


def test_a_body_longer_than_declared_does_not_report_past_the_end():
    assert P.describe_bytes(7_000_000_000, 6_000_000_000).startswith("100%")


def test_each_emitter_throttles_at_its_own_rate():
    """A line appended to a build log costs a write; an edit to a Telegram
    message is rate-limited by the API. One interval cannot serve both."""
    now = 0.0
    fast: list[int] = []
    slow: list[int] = []
    to_log = P.throttled(
        lambda done, total: fast.append(done), clock=lambda: now, interval=1.0,
    )
    to_chat = P.throttled(
        lambda done, total: slow.append(done), clock=lambda: now, interval=10.0,
    )

    for i in range(21):
        now = float(i)
        to_log(i, 100)
        to_chat(i, 100)

    assert len(fast) == 21
    assert slow == [0, 10, 20]


def test_the_log_sink_writes_a_line_and_passes_the_bytes_on(tmp_path):
    """A front end's sink rides along untouched: without a build-log line a
    multi-gigabyte transfer is two lines, one before and one after."""
    now = 0.0
    lines: list[str] = []
    downstream: list[tuple[int, int | None]] = []
    report = P.logged(
        "Downloading the base cloud image",
        lines.append,
        lambda done, total: downstream.append((done, total)),
    )

    report(2_100_000_000, 6_000_000_000)

    assert lines == [
        "Downloading the base cloud image: 35% (2.1 of 6.0 GB)",
    ]
    assert downstream == [(2_100_000_000, 6_000_000_000)]


def test_the_log_sink_works_with_no_front_end_behind_it():
    lines: list[str] = []
    P.logged("Downloading the control initramfs", lines.append)(
        32_000_000, 64_000_000,
    )

    assert lines == ["Downloading the control initramfs: 50% (32 of 64 MB)"]


# -- one fetcher per cached asset ---------------------------------------------
#
# The racers are separate OS processes: an orphaned ``openshrimp sandbox
# prefetch`` left behind by a setup wizard, and the core downloading on a
# first turn.  Every test below therefore spawns real interpreters — a
# threaded version of the same test passes against an in-process lock, which
# is exactly the lock that would fix nothing.
#
# The ``msvcrt.locking`` leg of :func:`prefetch.exclusive` is the one with the
# retry loop around a ten-second give-up, and nothing here executes it: on a
# POSIX runner the ``fcntl`` leg is the only one imported.  That branch is
# verified by hand on a Windows host or not at all.


@dataclasses.dataclass
class _Origin:
    """A local HTTP origin serving one asset, stalling halfway through a body.

    The stall is what forces the overlap.  Two fetchers that merely start
    close together pass against no lock at all when the timing is kind.
    """

    url: str
    requests: list[str]
    begun: list[str]
    release: threading.Event


@contextlib.contextmanager
def _serving(bodies: dict[str, list[bytes]]):
    """Serve *bodies*, one entry per request, until the caller releases them.

    A path may map to several bodies so that two requests for one URL can be
    told apart in the file they leave behind.  The last is repeated once the
    list runs out.
    """
    state = _Origin(url="", requests=[], begun=[], release=threading.Event())
    guard = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            name = self.path.lstrip("/")
            with guard:
                state.requests.append(name)
                queue = bodies.get(name)
                body = queue.pop(0) if queue and len(queue) > 1 else (
                    queue[0] if queue else None
                )
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if name.endswith(".sha256"):
                self.wfile.write(body)
                return
            half = len(body) // 2
            self.wfile.write(body[:half])
            self.wfile.flush()
            with guard:
                state.begun.append(name)
            state.release.wait(30)
            self.wfile.write(body[half:])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _sha256_line(body: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(body).hexdigest()}  {name}\n".encode()


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script, *args])


def _wait_for(predicate, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the other process")


#: Fetches a released asset the way the HCS backend does, against a local
#: origin.  It calls the real ``ensure_asset``, so the presence check under
#: the lock and the checksum are the ones that ship.
_FETCH = """
import sys
from pathlib import Path
from open_shrimp.sandbox import hcs_assets as A

base, dest, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
A.release_asset_url = lambda asset: base + "/" + asset
marker.write_text("reached")
A.ensure_asset("thing.img", dest, description="the thing")
"""

#: Two fetchers writing one fixed temporary, which is what the per-pid
#: staging name exists to prevent.
_SHARED_TEMPORARY = """
import sys
from pathlib import Path
from open_shrimp.sandbox.prefetch import stream_to_file

url, tmp, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
marker.write_text("reached")
stream_to_file(url, tmp)
"""

#: Holds the lock on an asset until told to let go.
_HOLD = """
import sys, time
from pathlib import Path
from open_shrimp.sandbox.prefetch import exclusive

dest, held, stop = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
with exclusive(dest):
    held.write_text("held")
    while not stop.exists():
        time.sleep(0.02)
"""


def test_the_second_process_to_want_an_asset_downloads_nothing(tmp_path):
    """The assertion that matters is not that the file is correct — that
    passes with no lock at all when the timing is kind.  It is that the
    second fetcher issues no request: it blocks, wakes once the winner has
    landed the file, sees it there, and returns."""
    body = b"rootfs bytes " * 4000
    dest = tmp_path / "thing.img"

    with _serving({
        "thing.img": [body],
        "thing.img.sha256": [_sha256_line(body, "thing.img")],
    }) as origin:
        first = _spawn(_FETCH, origin.url, str(dest), str(tmp_path / "a"))
        _wait_for(lambda: origin.begun)

        second = _spawn(_FETCH, origin.url, str(dest), str(tmp_path / "b"))
        # The marker says the loser has reached the fetch; from there it is
        # either blocked on the lock or has issued the request that fails
        # this test.
        _wait_for(lambda: (tmp_path / "b").exists())
        time.sleep(0.5)
        origin.release.set()

        assert first.wait(timeout=60) == 0
        assert second.wait(timeout=60) == 0

        assert origin.requests.count("thing.img") == 1

    assert dest.read_bytes() == body
    # Neither process leaves a staging file behind, and the lock file stays:
    # unlinking it would let two processes lock two inodes under one name.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "a", "b", "thing.img", "thing.img.lock",
    ]


def test_a_lock_already_held_is_reported_before_it_is_waited_on(tmp_path):
    """A caller that blocks in silence looks like a caller that has hung, so
    the wait is announced before it starts and reported again after it ends."""
    dest = tmp_path / "thing.img"
    held, stop = tmp_path / "held", tmp_path / "stop"

    holder = _spawn(_HOLD, str(dest), str(held), str(stop))
    try:
        _wait_for(held.exists)
        said: list[str] = []
        threading.Timer(0.5, stop.touch).start()

        with P.exclusive(dest, on_wait=lambda: said.append("waiting")) as waited:
            assert waited is True
            assert said == ["waiting"]
    finally:
        stop.touch()
        holder.wait(timeout=30)

    # Uncontended, there is nothing to announce.
    said = []
    with P.exclusive(dest, on_wait=lambda: said.append("waiting")) as waited:
        assert waited is False
    assert said == []


def test_two_fetchers_sharing_one_temporary_corrupt_it(tmp_path):
    """Why the staging name carries a pid.  Both fetchers here count their
    own bytes and both reach their declared length, so both believe they
    succeeded — and the file is neither of the two bodies.  A lock that some
    filesystem declines to honour degrades to a wasted download instead of
    this."""
    # Comfortably past the write buffer, so the bytes reach the file while
    # the other process is still writing rather than all at once on close.
    # Different lengths, so the interleaving is visible in the result no
    # matter which of the two finishes last.
    longer, shorter = b"A" * 524288, b"B" * 262144
    tmp = tmp_path / "thing.img.download"

    with _serving({"thing.img": [longer, shorter]}) as origin:
        url = f"{origin.url}/thing.img"
        first = _spawn(_SHARED_TEMPORARY, url, str(tmp), str(tmp_path / "a"))
        _wait_for(lambda: origin.begun)

        second = _spawn(_SHARED_TEMPORARY, url, str(tmp), str(tmp_path / "b"))
        _wait_for(lambda: len(origin.begun) >= 2)
        origin.release.set()

        assert first.wait(timeout=60) == 0
        assert second.wait(timeout=60) == 0

    landed = tmp.read_bytes()
    assert landed != longer
    assert landed != shorter


def test_a_staging_file_from_a_dead_fetcher_is_swept(tmp_path):
    """A per-pid temporary is not self-cleaning: the next run writes a
    different path rather than truncating the one an earlier run
    abandoned."""
    dest = tmp_path / "thing.img"
    abandoned = tmp_path / "thing.img.999999.download"
    abandoned.write_bytes(b"half an image")
    mine = P.staging_path(dest)
    mine.write_bytes(b"in flight")
    bystander = tmp_path / "unrelated.img.1234.download"
    bystander.write_bytes(b"another asset entirely")

    P.sweep_staging(dest)

    assert not abandoned.exists()
    assert mine.read_bytes() == b"in flight"
    assert bystander.exists()


def test_a_filesystem_that_will_not_lock_fetches_anyway(tmp_path, monkeypatch):
    """A share that does not implement locks would otherwise turn a download
    that worked into a failure.  The per-pid staging name is what keeps two
    unsynchronised fetchers out of each other's file."""
    def refuse(fd, *, blocking):
        raise P.Unlockable(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(P, "_acquire", refuse)
    dest = tmp_path / "thing.img"
    entered = False

    with P.exclusive(dest) as waited:
        entered = True
        assert waited is False

    assert entered


@pytest.mark.skipif(sys.platform == "win32", reason="the fcntl leg")
def test_a_lock_error_that_is_not_contention_is_not_retried(monkeypatch):
    """Retrying an error that will never clear spins a core forever, which is
    what a give-up-and-retry loop around a lock invites.  The ``msvcrt`` leg
    makes the same distinction against ``EDEADLOCK``, and is exercised only on
    a Windows host."""
    calls: list[bool] = []

    def broken(fd, flags):
        calls.append(True)
        raise OSError(errno.EBADF, "bad file descriptor")

    monkeypatch.setattr(P.fcntl, "flock", broken)

    with pytest.raises(P.Unlockable):
        P._acquire(0, blocking=True)
    assert len(calls) == 1


def test_the_sweep_leaves_a_similarly_named_asset_alone(tmp_path):
    """``rootfs.vhdx`` and ``rootfs.vhdx.gui`` are guarded by different locks,
    so a staging file belonging to the second may be a live download."""
    dest = tmp_path / "rootfs.vhdx"
    neighbour = tmp_path / "rootfs.vhdx.gui.4321.download"
    neighbour.write_bytes(b"a different asset, mid-flight")
    stale = tmp_path / "rootfs.vhdx.999999.download"
    stale.write_bytes(b"half an image")

    P.sweep_staging(dest)

    assert neighbour.exists()
    assert not stale.exists()
