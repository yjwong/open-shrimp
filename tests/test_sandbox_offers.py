"""What the setup wizard may offer as isolation, and on which host.

The one question the import step asks.  Offering a backend this machine
cannot start would write a config that fails on its first turn, far from the
wizard that could still have picked another — so the platform filter and the
prerequisite probe are the whole contract here.

The probes themselves are replaced: ``docker info`` and a libvirt connection
are neither fast nor the same answer on two machines, and what is under test
is which backends reach a person rather than what Docker said today.
"""

from __future__ import annotations

import json

import pytest

from open_shrimp import doctor
from open_shrimp.main import _run_sandboxes


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
        ("Linux", ["docker", "libvirt"]),
        ("Darwin", ["lima"]),
        ("Windows", ["hcs"]),
    ],
)
def test_only_what_the_platform_can_run_is_offered(
    monkeypatch: pytest.MonkeyPatch, all_pass: None, system: str, expected: list[str]
) -> None:
    _on(monkeypatch, system)

    assert [offer.backend for offer in doctor.sandbox_offers(None)] == expected


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
    assert offers["docker"].available
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
