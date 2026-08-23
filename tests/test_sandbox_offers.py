"""What a context may be isolated with, and on which host.

Two audiences, and the difference between them is the whole point.  The
config Mini App is owed every backend this platform can run; setup is owed
one, because the person answering it cannot weigh one hypervisor against
another.  Offering a backend this machine cannot start would write a config
that fails
on its first turn, far from the wizard that could still have said so — so
the platform filter and the prerequisite probe are the contract for both.

The probes themselves are replaced: a libvirt connection and a ``limactl``
lookup are neither fast nor the same answer on two machines, and what is under
test is which backends reach a person rather than what this host said today.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import replace

import pytest

from open_shrimp import doctor
from open_shrimp.config import _SANDBOX_BACKENDS
from open_shrimp.main import _run_sandboxes
from open_shrimp.sandbox import manager
from open_shrimp.sandbox.prerequisites import DECLARATIONS


@pytest.fixture
def all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor, "run_check", lambda label, check, config: doctor.Outcome(label, True, "ok")
    )


def _on(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: system)


@pytest.mark.parametrize(
    "system,expected",
    [
        ("Linux", ["libvirt"]),
        ("Darwin", ["lima"]),
        ("Windows", ["hcs"]),
    ],
)
def test_only_what_the_platform_can_run_is_offered(
    monkeypatch: pytest.MonkeyPatch, all_pass: None, system: str, expected: list[str]
) -> None:
    _on(monkeypatch, system)

    assert [offer.backend for offer in doctor.sandbox_offers(None)] == expected


def test_every_backend_the_config_accepts_reaches_a_wizard(
    monkeypatch: pytest.MonkeyPatch, all_pass: None
) -> None:
    """The offer list is the config's own registry, not a second copy of it.

    A backend added to ``_SANDBOX_BACKENDS`` and left out of the offers would
    be a choice the wizard silently never shows, with nothing failing — so the
    two are pinned equal here rather than kept equal by hand.
    """
    offered: set[str] = set()
    for system in ("Linux", "Darwin", "Windows"):
        _on(monkeypatch, system)
        offered |= {offer.backend for offer in doctor.sandbox_offers(None)}

    assert offered == _SANDBOX_BACKENDS


def test_every_backend_has_exactly_one_prerequisite_declaration() -> None:
    declared = [
        backend for backend, _platform, _checks in DECLARATIONS
    ]

    assert len(declared) == len(set(declared))
    assert set(declared) == set(manager._MANAGER_FACTORIES)
    assert set(declared) == _SANDBOX_BACKENDS


def test_every_prerequisite_leaf_imports_on_this_host() -> None:
    for module in ("libvirt_prereq", "lima_prereq", "hcs_prereq"):
        imported = importlib.import_module(f"open_shrimp.sandbox.{module}")
        assert imported.DECLARATION


def test_a_backend_that_cannot_start_here_keeps_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reported rather than dropped: the wizard names it as unavailable, and a
    user who wants it needs to be told what to install."""
    _on(monkeypatch, "Linux")

    def _run(label: str, check: object, config: object) -> doctor.Outcome:
        if label == "virtiofsd":
            return doctor.Outcome(label, False, "not found — install virtiofsd")
        return doctor.Outcome(label, True, "ok")

    monkeypatch.setattr(doctor, "run_check", _run)

    offers = {offer.backend: offer for offer in doctor.sandbox_offers(None)}
    assert not offers["libvirt"].available
    assert "install virtiofsd" in offers["libvirt"].detail


def test_one_failing_prerequisite_fails_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """libvirt needs both libvirt and virtiofsd, so a pass on one is not a
    backend that runs."""
    _on(monkeypatch, "Linux")
    monkeypatch.setattr(
        doctor,
        "run_check",
        lambda label, check, config: doctor.Outcome(
            label, None, "this check could not run"
        ),
    )

    assert not any(offer.available for offer in doctor.sandbox_offers(None))


@pytest.mark.parametrize(
    "system,expected",
    [("Linux", "libvirt"), ("Darwin", "lima"), ("Windows", "hcs")],
)
def test_setup_is_given_one_backend_per_platform(
    monkeypatch: pytest.MonkeyPatch, all_pass: None, system: str, expected: str
) -> None:
    """Setup asks whether a project is isolated, not which hypervisor isolates
    it — so there is exactly one answer per platform to render as a toggle."""
    _on(monkeypatch, system)

    offer = doctor.blessed_offer(None)
    assert offer is not None
    assert offer.backend == expected
    assert offer.available


def test_the_blessed_backend_is_one_the_platform_can_run(
    monkeypatch: pytest.MonkeyPatch, all_pass: None
) -> None:
    """A blessed backend the platform filter drops is a toggle that can never
    be turned on, with nothing failing to say so."""
    for system in ("Linux", "Darwin", "Windows"):
        _on(monkeypatch, system)
        assert doctor.blessed_backend() in {
            offer.backend for offer in doctor.sandbox_offers(None)
        }


def test_a_platform_with_no_blessed_backend_offers_nothing(
    monkeypatch: pytest.MonkeyPatch, all_pass: None
) -> None:
    """Answered rather than guessed at: a wizard on an unknown platform shows
    no sandbox row, which is not the same as showing a broken one."""
    _on(monkeypatch, "FreeBSD")

    assert doctor.blessed_backend() is None
    assert doctor.blessed_offer(None) is None


def test_the_shared_promise_names_no_mechanism(
    monkeypatch: pytest.MonkeyPatch, all_pass: None
) -> None:
    """One sentence goes to every platform, so it may only claim what is true
    on all of them — and the safest way to hold that is to claim no mechanism.

    Naming one also explains "sandbox" with a word no less technical, which
    is the failure this question was reshaped to avoid.  Pinned rather than
    left to review because a mechanism reads as helpful detail while it is
    being written.
    """
    for system in ("Linux", "Darwin", "Windows"):
        _on(monkeypatch, system)
        assert doctor.blessed_backend() is not None

    lowered = doctor.SANDBOX_SUMMARY.lower()
    for word in ("virtual machine", "vm", "container", "hypervisor", "lima",
                 "libvirt", "hyper-v", "linux"):
        assert word not in lowered, f"the shared note names a mechanism: {word!r}"


def test_every_case_of_the_sandbox_note_says_something(
    monkeypatch: pytest.MonkeyPatch, all_pass: None
) -> None:
    """The three front ends render this and branch on nothing, so an empty
    string in any case is a row that reads as a missing feature."""
    _on(monkeypatch, "Linux")
    ready = doctor.blessed_offer(None)
    assert ready is not None

    unavailable = replace(ready, available=False, detail="libvirt-python not installed")
    notes = [doctor.sandbox_note(o) for o in (ready, unavailable, None)]

    assert notes[0] == doctor.SANDBOX_SUMMARY
    assert "libvirt-python not installed" in notes[1]
    # Distinct from each other: a host with no sandbox and one with a broken
    # sandbox are different facts, and the note is the only thing that says so.
    assert len(set(notes)) == 3
    assert all(note.endswith(".") for note in notes)


def test_the_blessed_offer_carries_the_remedy_it_cannot_honour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The toggle goes off and unchangeable, not absent — a row that is simply
    missing reads as a missing feature instead of a missing prerequisite."""
    _on(monkeypatch, "Linux")
    monkeypatch.setattr(
        doctor,
        "run_check",
        lambda label, check, config: doctor.Outcome(
            label, label != "virtiofsd", "not found — install virtiofsd"
        ),
    )

    offer = doctor.blessed_offer(None)
    assert offer is not None and not offer.available
    assert "install virtiofsd" in offer.detail


class TestPendingDownloadsAreNotFailures:
    """A prerequisite the backend fetches itself must not deny the sandbox.

    The wizard's toggle is gated on ``available``, so a check that reports a
    missing-but-downloadable component as a failure sends a user to install
    by hand what the product installs for them — and on macOS, where Lima is
    on almost no machine by default, that is every user.
    """

    def test_a_downloadable_limactl_still_offers_the_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch, "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(
            "open_shrimp.sandbox.lima_helpers.find_limactl", lambda: None
        )

        outcome = doctor.prerequisites("lima", None)[0]
        assert outcome.ok, "a limactl that will be downloaded is not a failure"
        assert "downloaded on first use" in outcome.detail

        offer = doctor.blessed_offer(None)
        assert offer is not None and offer.available
        # The note is what the wizard shows, so it must not send anybody to
        # Homebrew for something about to be fetched for them.
        assert "brew" not in doctor.sandbox_note(offer)

    def test_a_platform_lima_publishes_nothing_for_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There the download has nothing to fetch, so Homebrew is the only
        route left and naming it is the whole point of the remedy."""
        _on(monkeypatch, "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "ppc64")
        monkeypatch.setattr(
            "open_shrimp.sandbox.lima_helpers.find_limactl", lambda: None
        )

        offer = doctor.blessed_offer(None)
        assert offer is not None and not offer.available
        assert "brew install lima" in doctor.sandbox_note(offer)

    def test_a_limactl_in_the_managed_bin_dir_counts_as_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing puts the managed bin directory on ``$PATH``, so a check
        that only consulted ``$PATH`` would report a downloaded limactl as
        missing and re-download it forever."""
        _on(monkeypatch, "Darwin")
        monkeypatch.setattr(
            "open_shrimp.sandbox.lima_helpers.find_limactl",
            lambda: "/managed/bin/limactl",
        )

        outcome = doctor.prerequisites("lima", None)[0]
        assert outcome.ok
        assert "/managed/bin/limactl" in outcome.detail


def test_the_json_form_is_parsable(
    monkeypatch: pytest.MonkeyPatch, all_pass: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two GUI wizards cannot call Python, so this is how they ask."""
    _on(monkeypatch, "Darwin")
    # Neither this machine's config nor its data directory is the subject:
    # leaving them alone keeps the global path state other tests share intact.
    monkeypatch.setattr(doctor, "_load_config", lambda path: None)
    monkeypatch.setattr("open_shrimp.main.init_paths", lambda name: None)

    assert _run_sandboxes(json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sandboxes"] == [
        {
            "backend": "lima",
            "label": "Lima",
            "summary": "each project runs in its own Linux virtual machine",
            "available": True,
            "detail": "",
        }
    ]
    # Everything a GUI needs to draw one toggle, already resolved: what to
    # write, whether it can be ticked, and what to say.  A front end that had
    # to derive any of these would be a second answer to a question the core
    # already answers for the terminal wizard, free to drift from it.
    assert payload["sandbox"] == {
        "backend": "lima",
        "available": True,
        "note": doctor.SANDBOX_SUMMARY,
    }


def test_the_json_names_no_backend_a_host_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GUI ticks the box from ``available`` alone, so a backend named beside
    a false one must never be written — and the note has to say why."""
    _on(monkeypatch, "Windows")
    monkeypatch.setattr(doctor, "_load_config", lambda path: None)
    monkeypatch.setattr("open_shrimp.main.init_paths", lambda name: None)
    monkeypatch.setattr(
        doctor,
        "run_check",
        lambda label, check, config: doctor.Outcome(label, False, "Hyper-V is off"),
    )

    assert _run_sandboxes(json_output=True) == 0

    sandbox = json.loads(capsys.readouterr().out)["sandbox"]
    assert sandbox["backend"] == "hcs"
    assert not sandbox["available"]
    assert "Hyper-V is off" in sandbox["note"]
