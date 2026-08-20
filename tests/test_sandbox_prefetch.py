"""Pre-fetching a sandbox backend's shared assets, and the progress it reports.

The download itself is the least interesting part.  What matters is the shape
of what a front end reads: an unknown size is an absent ``total`` rather than
a zero one, an asset already on disk costs nothing, the byte counter stays
exact while the reporting stays coarse, and a disk that cannot hold the
result says so before the first byte moves.
"""

from __future__ import annotations

import io
import json
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


def test_a_backend_with_nothing_to_fetch_still_finishes(monkeypatch):
    """Docker builds its image rather than fetching one, so its prefetch is
    empty — and an empty run must still close the stream a front end waits on."""
    seen: list[dict] = []
    P.prefetch("docker", emit=seen.append)

    assert seen == [{"state": "finished"}]


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
