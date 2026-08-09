"""``openshrimp doctor``'s HCS checks.

The facts they probe are Windows-only, so the probes themselves are faked the
way the other HCS tests fake them (``hcs_win`` swapped into ``sys.modules``,
paths redirected through the environment variables the backend honours).  What
is tested is the contract the operator sees: which checks run on which host,
what a missing piece is called, and that the remedy named matches the one
``hcs.ensure_environment`` raises at boot.
"""

from __future__ import annotations

import io
import sys

import pytest

from open_shrimp import doctor, paths
from open_shrimp import sandbox as sandbox_pkg
from open_shrimp.config import Config, ContextConfig, SandboxConfig, TelegramConfig


def _config(**sandboxes: SandboxConfig | None) -> Config:
    """A config whose contexts are exactly the named sandboxes."""
    return Config(
        telegram=TelegramConfig(token="t"),
        allowed_users=[1],
        contexts={
            name: ContextConfig(
                directory="/w", description=name, allowed_tools=[], sandbox=sb,
            )
            for name, sb in sandboxes.items()
        },
        default_context=next(iter(sandboxes), "default"),
    )


def _fake_hcs_win(monkeypatch, permitted: bool | OSError) -> None:
    """Swap in an ``hcs_win`` whose only fact is the rights probe.

    The package attribute is set too, not just ``sys.modules``: on a host
    where the real module imports, ``from ... import hcs_win`` resolves the
    attribute first and would reach the live probe.
    """
    def _probe() -> bool:
        if isinstance(permitted, OSError):
            raise permitted
        return permitted

    module = type(sys)("open_shrimp.sandbox.hcs_win")
    module.can_manage_compute_systems = _probe
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", module)
    monkeypatch.setattr(sandbox_pkg, "hcs_win", module, raising=False)


# -- which checks run where ---------------------------------------------------


def test_a_non_windows_host_runs_no_hcs_check(monkeypatch, capsys):
    paths.init_paths()
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor, "_load_config", lambda path: None)
    doctor.run_doctor()

    printed = capsys.readouterr().out
    for label in (
        "win32more", "Hyper-V rights", "HCS kernel", "HCS control initramfs",
        "csc", "HCS base image", "HCS RDP helper",
    ):
        assert label not in printed


def test_a_windows_host_runs_every_hcs_check(monkeypatch, capsys):
    # Nothing Windows exists here, so every host-wide HCS check fails — the
    # point is that they run, and report, instead of raising on import.
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor, "_load_config", lambda path: None)
    monkeypatch.delenv("OPENSHRIMP_HCS_RDP_HELPER", raising=False)
    assert doctor.run_doctor() == 1

    printed = capsys.readouterr().out
    for label in (
        "win32more", "Hyper-V rights", "HCS kernel", "HCS control initramfs",
        "csc", "HCS base image", "HCS RDP helper",
    ):
        assert f"{label}:" in printed
    # A Linux-only check does not leak onto a Windows host.
    assert "virtiofsd" not in printed


# -- Hyper-V rights -----------------------------------------------------------


def test_a_privileged_token_passes(monkeypatch):
    _fake_hcs_win(monkeypatch, permitted=True)
    ok, detail = doctor._check_hyperv_rights(None)
    assert ok
    assert "Hyper-V Administrators" in detail


def test_an_unprivileged_token_fails_with_the_hresult_and_the_remedy(monkeypatch):
    _fake_hcs_win(monkeypatch, permitted=False)
    ok, detail = doctor._check_hyperv_rights(None)
    assert not ok
    # The bare HRESULT is what the operator would otherwise meet at create.
    assert "0x8037011B" in detail
    assert "elevated" in detail
    assert "Hyper-V Administrators" in detail


def test_a_probe_that_cannot_answer_is_not_a_failure(monkeypatch):
    # Same rule the backend's preflight holds to: a broken probe must not
    # ground a host that would have worked.
    _fake_hcs_win(monkeypatch, permitted=OSError("no token"))
    ok, detail = doctor._check_hyperv_rights(None)
    assert ok
    assert "no token" in detail


def test_rights_cannot_be_probed_without_win32more(monkeypatch):
    monkeypatch.delattr(sandbox_pkg, "hcs_win", raising=False)
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", None)
    ok, detail = doctor._check_hyperv_rights(None)
    assert not ok
    assert "win32more" in detail


# -- kernel and control initramfs ---------------------------------------------


def test_the_kernel_is_found_where_the_environment_points(monkeypatch, tmp_path):
    kernel = tmp_path / "kernel"
    kernel.write_bytes(b"")
    monkeypatch.setenv("OPENSHRIMP_HCS_KERNEL", str(kernel))
    ok, detail = doctor._check_hcs_kernel(None)
    assert ok
    assert str(kernel) in detail


def test_a_missing_kernel_names_wsl_and_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENSHRIMP_HCS_KERNEL", str(tmp_path / "absent"))
    ok, detail = doctor._check_hcs_kernel(None)
    assert not ok
    assert "WSL" in detail
    assert "OPENSHRIMP_HCS_KERNEL" in detail


def test_the_initramfs_is_found_where_the_environment_points(monkeypatch, tmp_path):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(b"")
    monkeypatch.setenv("OPENSHRIMP_HCS_INITRD", str(initrd))
    ok, detail = doctor._check_hcs_initrd(None)
    assert ok
    assert str(initrd) in detail


def test_an_override_pointing_at_nothing_is_the_one_failure(
    monkeypatch, tmp_path,
):
    """The override suppresses the download, so it is the only way to have no
    path to an initramfs at all."""
    monkeypatch.setenv("OPENSHRIMP_HCS_INITRD", str(tmp_path / "absent"))
    ok, detail = doctor._check_hcs_initrd(None)
    assert not ok
    assert "OPENSHRIMP_HCS_INITRD" in detail
    assert "scripts/build_hcs_initrd.sh" in detail


def test_an_unstaged_initramfs_reports_the_pending_download(
    monkeypatch, tmp_path,
):
    """Nothing is fetched by a check, so an absent initramfs passes on the
    strength of the download the backend will do."""
    monkeypatch.delenv("OPENSHRIMP_HCS_INITRD", raising=False)
    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs.initrd_path", lambda: tmp_path / "absent",
    )
    ok, detail = doctor._check_hcs_initrd(None)
    assert ok
    assert "openshrimp-hcs-initrd.img" in detail


# -- csc ----------------------------------------------------------------------


def test_csc_is_found_where_the_override_points(monkeypatch, tmp_path):
    csc = tmp_path / "csc.exe"
    csc.write_bytes(b"")
    monkeypatch.setenv("OPENSHRIMP_HCS_CSC", str(csc))
    ok, detail = doctor._check_csc(None)
    assert ok
    assert str(csc) in detail


def test_a_missing_csc_says_what_it_builds(monkeypatch):
    # A host that has the in-box compiler cannot be talked out of it, so the
    # absence is injected where the backend reports it.
    def _absent() -> str:
        raise RuntimeError(
            "csc.exe (in-box .NET Framework compiler) not found — expected "
            r"under C:\Windows\Microsoft.NET\Framework64\v4.0.30319."
        )

    monkeypatch.setattr("open_shrimp.sandbox.hcs.find_csc", _absent)
    ok, detail = doctor._check_csc(None)
    assert not ok
    assert "Framework64" in detail
    assert "launcher" in detail
    # One sentence, not two run together by the error's own full stop.
    assert ". (" not in detail


# -- base image (per context) -------------------------------------------------


def _hcs(tmp_path, **kwargs) -> SandboxConfig:
    image = tmp_path / "root.vhdx"
    image.write_bytes(b"")
    return SandboxConfig(backend="hcs", base_image=str(image), **kwargs)


def test_a_staged_base_image_passes_and_names_the_context(tmp_path):
    ok, detail = doctor._check_hcs_base_image(_config(work=_hcs(tmp_path)))
    assert ok
    assert "work" in detail
    assert "root.vhdx" in detail


def test_a_base_image_that_is_not_there_fails(tmp_path):
    config = _config(work=SandboxConfig(backend="hcs", base_image="C:/absent.vhdx"))
    ok, detail = doctor._check_hcs_base_image(config)
    assert not ok
    assert "work" in detail
    assert "C:/absent.vhdx" in detail


def test_an_hcs_context_with_no_base_image_reports_the_pending_download(
    tmp_path, monkeypatch,
):
    """No ``base_image`` is the ordinary case, not a problem: the released
    rootfs is downloaded on first boot."""
    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs_assets.asset_dir", lambda: tmp_path,
    )
    ok, detail = doctor._check_hcs_base_image(_config(work=SandboxConfig(backend="hcs")))
    assert ok
    assert "openshrimp-hcs-base-rootfs.vhdx.zst" in detail


def test_a_computer_use_context_with_no_base_image_names_the_gui_asset(
    tmp_path, monkeypatch,
):
    """The desktop image is fetched instead of the base one, not alongside."""
    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs_assets.asset_dir", lambda: tmp_path,
    )
    config = _config(work=SandboxConfig(backend="hcs", computer_use=True))
    ok, detail = doctor._check_hcs_base_image(config)
    assert ok
    assert "openshrimp-hcs-gui-rootfs.vhdx.zst" in detail
    assert "base-rootfs" not in detail


def test_computer_use_also_needs_the_baked_gui_template(tmp_path):
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_base_image(config)
    assert not ok
    assert "scripts/build_hcs_gui_rootfs.sh" in detail


def test_a_baked_gui_template_satisfies_computer_use(tmp_path, monkeypatch):
    gui = tmp_path / "root-gui.vhdx"
    gui.write_bytes(b"")
    # gui_image_path maps names the Windows way; the check consults it rather
    # than deriving the name itself.
    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs_helpers.gui_image_path", lambda base: str(gui),
    )
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_base_image(config)
    assert ok
    assert "computer use" in detail


def test_a_host_with_no_hcs_context_has_nothing_to_check(tmp_path):
    config = _config(work=SandboxConfig(backend="docker"))
    ok, detail = doctor._check_hcs_base_image(config)
    assert ok
    assert "nothing to check" in detail


def test_an_unreadable_config_leaves_the_per_context_checks_idle():
    for check in (doctor._check_hcs_base_image, doctor._check_hcs_rdp_helper):
        ok, detail = check(None)
        assert ok
        assert "no config loaded" in detail


def test_a_disabled_sandbox_is_not_an_hcs_context(tmp_path):
    config = _config(work=_hcs(tmp_path, enabled=False))
    assert doctor._hcs_sandboxes(config) == []


# -- RDP helper and its FreeRDP DLLs ------------------------------------------


def _bundle(tmp_path, *, dlls: bool) -> str:
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "hcs_rdp_helper.exe").write_bytes(b"")
    if dlls:
        for name in (
            "libfreerdp-client3.dll", "libfreerdp3.dll", "libwinpr3.dll",
        ):
            (d / name).write_bytes(b"")
    return str(d)


def _toolchain(tmp_path, *, complete: bool) -> str:
    d = tmp_path / "mingw64" / "bin"
    d.mkdir(parents=True)
    (d / "gcc.exe").write_bytes(b"")
    if complete:
        (d / "pkgconf.exe").write_bytes(b"")
        for name in (
            "libfreerdp-client3.dll", "libfreerdp3.dll", "libwinpr3.dll",
        ):
            (d / name).write_bytes(b"")
    return str(d)


def _no_bundle(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs_rdp.find_shipped_helper", lambda: None,
    )


def test_a_context_without_computer_use_needs_no_helper(tmp_path):
    ok, detail = doctor._check_hcs_rdp_helper(_config(work=_hcs(tmp_path)))
    assert ok
    assert "computer_use" in detail


def test_a_complete_prebuilt_bundle_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", _bundle(tmp_path, dlls=True))
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert ok
    assert "hcs_rdp_helper.exe" in detail


def test_a_helper_without_its_dlls_fails(tmp_path, monkeypatch):
    # The exe alone is not runnable: the loader resolves FreeRDP beside it.
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", _bundle(tmp_path, dlls=False))
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert not ok
    assert "libfreerdp" in detail
    assert "openshrimp-hcs-rdp-helper-windows-x86_64.zip" in detail


def test_a_helper_override_pointing_at_nothing_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSHRIMP_HCS_RDP_HELPER", str(tmp_path / "absent"))
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert not ok
    assert "OPENSHRIMP_HCS_RDP_HELPER" in detail


def test_a_toolchain_is_enough_when_no_bundle_is_staged(tmp_path, monkeypatch):
    _no_bundle(monkeypatch)
    config = _config(
        work=_hcs(tmp_path, computer_use=True,
                  mingw_bin=_toolchain(tmp_path, complete=True)),
    )
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert ok
    assert "buildable" in detail


def test_an_incomplete_toolchain_names_the_missing_packages(tmp_path, monkeypatch):
    _no_bundle(monkeypatch)
    config = _config(
        work=_hcs(tmp_path, computer_use=True,
                  mingw_bin=_toolchain(tmp_path, complete=False)),
    )
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert not ok
    assert "pkgconf.exe" in detail
    assert "-freerdp" in detail


def test_neither_a_bundle_nor_a_toolchain_names_both_ways_out(tmp_path, monkeypatch):
    _no_bundle(monkeypatch)
    config = _config(work=_hcs(tmp_path, computer_use=True))
    ok, detail = doctor._check_hcs_rdp_helper(config)
    assert not ok
    assert "openshrimp-hcs-rdp-helper-windows-x86_64.zip" in detail
    assert "mingw_bin" in detail


def test_the_check_never_downloads(tmp_path, monkeypatch):
    _no_bundle(monkeypatch)

    def _boom() -> None:
        raise AssertionError("doctor must not fetch the helper bundle")

    monkeypatch.setattr(
        "open_shrimp.sandbox.hcs_rdp.download_shipped_helper", _boom,
    )
    config = _config(work=_hcs(tmp_path, computer_use=True))
    assert not doctor._check_hcs_rdp_helper(config)[0]


# -- an output stream that cannot carry the icons ------------------------------


class _Stream(io.StringIO):
    """A stdout that refuses whatever its encoding cannot carry, the way a
    real one does, and that cannot be reconfigured out of it."""

    encoding = "utf-8"

    def write(self, text: str) -> int:
        text.encode(self.encoding)
        return super().write(text)


class _Cp1252Stdout(_Stream):
    encoding = "cp1252"


class _AsciiStdout(_Stream):
    encoding = "ascii"


class _Utf8Stdout(_Stream):
    encoding = "utf-8"


def test_a_windows_stdout_gets_a_report_it_can_encode(monkeypatch):
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor, "_load_config", lambda path: None)
    monkeypatch.delenv("OPENSHRIMP_HCS_RDP_HELPER", raising=False)
    out = _Cp1252Stdout()
    monkeypatch.setattr(sys, "stdout", out)

    assert doctor.run_doctor() == 1

    printed = out.getvalue()
    assert "✅" not in printed and "❌" not in printed
    assert "[ok]" in printed and "[!!]" in printed


def test_a_stream_that_takes_utf8_keeps_the_icons(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Utf8Stdout())
    assert doctor._icons() == ("✅", "❌")
    assert doctor._printable("kernel — install WSL") == "kernel — install WSL"


def test_a_stream_that_cannot_encode_a_character_gets_a_placeholder(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _AsciiStdout())
    assert "?" in doctor._printable("kernel — install WSL")


# -- win32more ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="win32more is installed there")
def test_win32more_missing_names_the_extra():
    ok, detail = doctor._check_win32more(None)
    assert not ok
    assert "--extra hcs" in detail
