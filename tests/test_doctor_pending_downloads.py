"""A component this project downloads for itself is not a missing prerequisite.

``run_doctor`` exits 1 when any check fails, so a check that reports a
pending download as a failure makes a brand-new, entirely healthy install
report itself broken — and tells the user to go and install something that
was about to arrive on its own.

The sandbox assets have always got this right (``_check_hcs_initrd``: "a
check must not fetch anything, so an absent initramfs passes on the strength
of that download").  moonshine-stt and cloudflared did not, and the opencode
CLI is the third component to arrive under the same rule.  It is pinned here
because it reads as strictness while it is being written, and only reads as a
bug from the far side of a fresh install.
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
    import open_shrimp.backend.opencode.binary as opencode_binary

    monkeypatch.setattr(doctor, "managed_binary", lambda name: None)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(opencode_binary, "managed_binary", lambda name: None)


def _opencode_config() -> doctor.Config:
    """The smallest config that selects opencode somewhere."""
    from open_shrimp.config import ContextConfig, TelegramConfig

    return doctor.Config(
        telegram=TelegramConfig(token="t"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="d",
                allowed_tools=[],
                model="openai/gpt-5.5",
            )
        },
        backend="opencode",
    )


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
    install exits 1.  Backends are excluded because libvirt and its kin are
    system packages this project cannot fetch, so their failures are real."""
    _on(monkeypatch, "Linux", "x86_64")

    fetched = [
        doctor.run_check(label, check, None)
        for label, check in doctor._CHECKS
    ]
    assert fetched, "the platform-agnostic checks are the subject of this test"
    assert all(outcome.ok for outcome in fetched), [
        (o.label, o.detail) for o in fetched if not o.ok
    ]


def test_opencode_is_silent_on_an_install_that_does_not_select_it(
    nothing_downloaded: None,
) -> None:
    """Most installs run the Claude backend, and reporting on a CLI nothing
    there runs sends their operator hunting a fault that isn't one."""
    ok, detail = doctor._check_opencode(None)
    assert ok
    assert "not selected" in detail


def test_opencode_selected_but_not_downloaded_passes(
    monkeypatch: pytest.MonkeyPatch, nothing_downloaded: None
) -> None:
    _on(monkeypatch, "Linux", "x86_64")

    ok, detail = doctor._check_opencode(_opencode_config())
    assert ok, "the CLI is fetched on first use and must not fail the doctor"
    assert "not downloaded yet" in detail


def test_opencode_on_a_platform_with_no_published_build_fails(
    monkeypatch: pytest.MonkeyPatch, nothing_downloaded: None
) -> None:
    # ``doctor.platform`` is the stdlib module itself, so patching it here is
    # also what ``release.host_slug`` sees.
    _on(monkeypatch, "Linux", "ppc64le")

    ok, detail = doctor._check_opencode(_opencode_config())
    assert not ok
    assert "ppc64le" in detail


def test_a_fresh_opencode_install_does_not_report_itself_broken(
    monkeypatch: pytest.MonkeyPatch, nothing_downloaded: None
) -> None:
    """The same rule as the claude-backed case, with opencode selected: a
    brand-new install has downloaded nothing and is entirely healthy."""
    _on(monkeypatch, "Linux", "x86_64")
    config = _opencode_config()

    outcomes = [
        doctor.run_check(label, check, config)
        for label, check in doctor._CHECKS
    ]
    assert all(outcome.ok for outcome in outcomes), [
        (o.label, o.detail) for o in outcomes if not o.ok
    ]
