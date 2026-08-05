"""Per-backend config validation for ``sandbox.backend: hcs``.

Covers the two ways an hcs context can be wrong at load time: a processor
count HCS will refuse at create, and a knob the backend has no code path for.
"""

from __future__ import annotations

import pytest

from open_shrimp.config import _parse, _validate_raw


def _hcs_raw(sandbox_extra: dict | None = None):
    sandbox: dict = {"backend": "hcs", "base_image": r"C:\images\root.vhdx"}
    if sandbox_extra:
        sandbox.update(sandbox_extra)
    return {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
                "sandbox": sandbox,
            }
        },
        "default_context": "default",
    }


# -- processor topology -------------------------------------------------------


@pytest.mark.parametrize("cpus", [2, 4, 6, 8, 16])
def test_even_cpu_counts_validate(cpus):
    _validate_raw(_hcs_raw({"cpus": cpus}))


@pytest.mark.parametrize("cpus", [1, 3, 5, 7, 15])
def test_odd_cpu_counts_are_rejected(cpus):
    with pytest.raises(ValueError, match="sandbox.cpus must be an even number"):
        _validate_raw(_hcs_raw({"cpus": cpus}))


@pytest.mark.parametrize("cpus", [0, -2])
def test_non_positive_cpu_counts_are_rejected(cpus):
    with pytest.raises(ValueError, match="at least 2"):
        _validate_raw(_hcs_raw({"cpus": cpus}))


def test_the_cpu_message_names_what_is_supported():
    with pytest.raises(ValueError) as exc:
        _validate_raw(_hcs_raw({"cpus": 3}))
    assert "2, 4, 6" in str(exc.value)


def test_the_default_cpu_count_is_supportable():
    cfg = _parse(_hcs_raw())
    assert cfg.contexts["default"].sandbox.cpus == 2
    _validate_raw(_hcs_raw())


def test_a_non_integer_cpu_count_still_reports_the_type_error():
    with pytest.raises(ValueError, match="must be an integer"):
        _validate_raw(_hcs_raw({"cpus": "4"}))


def test_odd_cpu_counts_are_fine_on_other_backends():
    raw = _hcs_raw()
    raw["contexts"]["default"]["sandbox"] = {"backend": "libvirt", "cpus": 3}
    _validate_raw(raw)


# -- knobs the backend ignores ------------------------------------------------


@pytest.mark.parametrize(
    "knob",
    [
        {"virgl": True},
        {"docker_in_docker": True},
        {"dockerfile": "Dockerfile.custom"},
        {"guest_os": "macos"},
        {"phone_use": True},
        {"android": {"image_type": "GAPPS"}},
        {"android": {}},
    ],
)
def test_unsupported_knobs_are_rejected(knob):
    key = next(iter(knob))
    with pytest.raises(ValueError, match=f"sandbox.{key} is not supported"):
        _validate_raw(_hcs_raw(knob))


def test_the_rejection_names_the_hcs_backend():
    with pytest.raises(ValueError, match="'hcs' backend"):
        _validate_raw(_hcs_raw({"virgl": True}))


@pytest.mark.parametrize(
    "knob",
    [
        {"virgl": False},
        {"docker_in_docker": False},
        {"dockerfile": None},
        {"guest_os": "linux"},
        {"phone_use": False},
        {"android": None},
    ],
)
def test_knobs_left_at_their_unset_value_are_inert(knob):
    # An operator who spells a default out loud is not asking for anything.
    _validate_raw(_hcs_raw(knob))


def test_a_bare_hcs_context_validates():
    _validate_raw(_hcs_raw())


def test_the_knobs_still_work_on_the_backends_that_implement_them():
    raw = _hcs_raw()
    raw["contexts"]["default"]["sandbox"] = {
        "backend": "docker", "virgl": True, "docker_in_docker": True,
        "dockerfile": "Dockerfile.custom",
    }
    _validate_raw(raw)
