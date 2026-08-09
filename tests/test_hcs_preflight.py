"""``ensure_environment``'s host-capability gate: Hyper-V management rights.

HCS refuses a caller in neither Administrators nor Hyper-V Administrators
with a bare ``0x8037011B`` at create time, so the backend asks the token
first.  The probe itself is Win32 (``CheckTokenMembership`` in ``hcs_win``)
and only a Windows host can exercise it; what is tested here is the contract
``hcs.py`` holds it to — when the gate closes, in what order, and what it
tells the operator.  ``hcs_win`` is faked (the shape the port-bridge and
computer-use tests use), so nothing Windows is imported.
"""

from __future__ import annotations

import sys

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.hcs import HcsSandbox


def _sandbox(tmp_path, monkeypatch, *, permitted: bool | OSError) -> HcsSandbox:
    """An HCS sandbox whose only injected Windows fact is the rights probe."""
    monkeypatch.setattr(sys, "platform", "win32")

    def _probe() -> bool:
        if isinstance(permitted, OSError):
            raise permitted
        return permitted

    module = type(sys)("open_shrimp.sandbox.hcs_win")
    module.can_manage_compute_systems = _probe
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", module)

    return HcsSandbox(
        "default",
        SandboxConfig(backend="hcs", base_image=str(tmp_path / "root.vhdx")),
        str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
    )


def _refusal(tmp_path, monkeypatch) -> str:
    sb = _sandbox(tmp_path, monkeypatch, permitted=False)
    with pytest.raises(RuntimeError) as exc:
        sb.ensure_environment()
    return str(exc.value)


# -- what the operator is told ------------------------------------------------


def test_an_unprivileged_token_is_refused_before_anything_is_built(
    tmp_path, monkeypatch,
):
    _refusal(tmp_path, monkeypatch)
    # The gate precedes every side effect, so nothing was staged on the way
    # to a host that cannot boot the sandbox at all.
    assert not (tmp_path / "state").exists()


def test_the_refusal_names_both_ways_to_satisfy_the_requirement(
    tmp_path, monkeypatch,
):
    message = _refusal(tmp_path, monkeypatch)
    assert "elevated" in message
    assert "Hyper-V Administrators" in message


def test_the_refusal_names_the_HRESULT_it_stands_in_for(tmp_path, monkeypatch):
    # The operator who already hit the raw create failure, or who searches
    # for it later, must land on the same message.
    assert "0x8037011B" in _refusal(tmp_path, monkeypatch)


# -- when the gate stays open -------------------------------------------------


def test_a_privileged_token_passes_to_the_next_preflight(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("OPENSHRIMP_HCS_KERNEL", str(tmp_path / "no-kernel"))
    sb = _sandbox(tmp_path, monkeypatch, permitted=True)
    with pytest.raises(RuntimeError, match="HCS kernel not found"):
        sb.ensure_environment()


def test_the_broken_probe_is_logged_for_the_operator(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("OPENSHRIMP_HCS_KERNEL", str(tmp_path / "no-kernel"))
    sb = _sandbox(
        tmp_path, monkeypatch, permitted=OSError(5, "OpenProcessToken failed"),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="HCS kernel not found"):
            sb.ensure_environment()
    assert "0x8037011B" in caplog.text
