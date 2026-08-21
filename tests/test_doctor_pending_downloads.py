"""A component this project downloads for itself is not a missing prerequisite.

``run_doctor`` exits 1 when any check fails, so a check that reports a
pending download as a failure makes a brand-new, entirely healthy install
report itself broken — and tells the user to go and install something that
was about to arrive on its own.

The sandbox assets have always got this right (``_check_hcs_initrd``: "a
check must not fetch anything, so an absent initramfs passes on the strength
of that download").  These two did not.  The rule is pinned here because it
reads as strictness while it is being written, and only reads as a bug from
the far side of a fresh install.
"""

from __future__ import annotations

import pytest

from open_shrimp import doctor


def _on(monkeypatch: pytest.MonkeyPatch, system: str, machine: str) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: system)
    monkeypatch.setattr(doctor.platform, "machine", lambda: machine)


@pytest.fixture
def nothing_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state of every install that has never transcribed or tunnelled."""
    monkeypatch.setattr(doctor, "managed_binary", lambda name: None)


@pytest.mark.parametrize(
    "label,check",
    [
        ("moonshine-stt", doctor._check_moonshine_stt),
        ("cloudflared", doctor._check_cloudflared),
    ],
)
def test_a_binary_that_will_be_downloaded_passes(
    monkeypatch: pytest.MonkeyPatch,
    nothing_downloaded: None,
    label: str,
    check,
) -> None:
    _on(monkeypatch, "Linux", "x86_64")

    ok, detail = check(None)
    assert ok, f"{label} is fetched on first use and must not fail the doctor"
    assert "not downloaded yet" in detail
    # The remedy is the absence of one: nothing to install by hand.
    assert "install" not in detail.lower()


@pytest.mark.parametrize(
    "label,check",
    [
        ("moonshine-stt", doctor._check_moonshine_stt),
        ("cloudflared", doctor._check_cloudflared),
    ],
)
def test_a_platform_with_no_published_build_still_fails(
    monkeypatch: pytest.MonkeyPatch,
    nothing_downloaded: None,
    label: str,
    check,
) -> None:
    """There the download has nothing to fetch, so it is a real failure and
    the remedy has to name the one route left."""
    _on(monkeypatch, "Linux", "ppc64le")

    ok, detail = check(None)
    assert not ok, f"{label} cannot arrive on its own here"
    assert "ppc64le" in detail


@pytest.mark.parametrize(
    "label,check",
    [
        ("moonshine-stt", doctor._check_moonshine_stt),
        ("cloudflared", doctor._check_cloudflared),
    ],
)
def test_an_already_downloaded_binary_reports_where_it_is(
    monkeypatch: pytest.MonkeyPatch, label: str, check
) -> None:
    monkeypatch.setattr(doctor, "managed_binary", lambda name: f"/managed/{name}")

    ok, detail = check(None)
    assert ok
    assert f"/managed/{label}" in detail


def test_a_fresh_install_does_not_report_itself_broken(
    monkeypatch: pytest.MonkeyPatch, nothing_downloaded: None
) -> None:
    """The whole point, stated once: neither check may be the reason a new
    install exits 1.  Backends are excluded because Docker and libvirt are
    system packages this project cannot fetch, so their failures are real."""
    _on(monkeypatch, "Linux", "x86_64")

    fetched = [
        doctor.run_check(label, check, None)
        for label, check, _plat, backends in doctor._CHECKS
        if not backends
    ]
    assert fetched, "the platform-agnostic checks are the subject of this test"
    assert all(outcome.ok for outcome in fetched), [
        (o.label, o.detail) for o in fetched if not o.ok
    ]
