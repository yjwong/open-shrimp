"""How an HCS sandbox resolves the kernel-adjacent guest artifacts.

Two decisions matter and neither involves the network, so nothing here
downloads: which path an artifact is looked for at, and which asset is chosen
when one has to be fetched.  ``ensure_asset`` is stubbed to record the ask.
"""

from __future__ import annotations

import sys

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs as hcs_mod
from open_shrimp.sandbox import hcs_assets
from open_shrimp.sandbox.hcs import HcsSandbox


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    """Point the managed cache at the test's own directory, so a developer
    machine's real cache neither satisfies nor is written by a test."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(hcs_assets, "asset_dir", lambda: cache)
    monkeypatch.delenv("OPENSHRIMP_HCS_INITRD", raising=False)
    return cache


@pytest.fixture
def asked(monkeypatch):
    """Record every ``ensure_asset`` call instead of performing it."""
    calls: list[tuple[str, str]] = []

    def fake_ensure(asset, dest, *, description, log=None, progress=None):
        calls.append((asset, str(dest)))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")
        return dest

    monkeypatch.setattr(hcs_assets, "ensure_asset", fake_ensure)
    return calls


def _sandbox(tmp_path, monkeypatch, **config_extra) -> HcsSandbox:
    monkeypatch.setattr(sys, "platform", "win32")
    defaults: dict = {"backend": "hcs"}
    defaults.update(config_extra)
    return HcsSandbox(
        "default", SandboxConfig(**defaults), str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
    )


# -- the control initramfs ----------------------------------------------------


def test_a_staged_initramfs_wins_over_a_download(monkeypatch, tmp_path, asked):
    staged = tmp_path / "programdata-initrd.img"
    staged.write_bytes(b"staged")
    monkeypatch.setattr(hcs_mod, "_DEFAULT_INITRD", str(staged))

    assert hcs_mod.initrd_path() == staged
    assert hcs_mod.ensure_initrd() == staged
    assert asked == []


def test_the_override_wins_over_both(monkeypatch, tmp_path, asked):
    staged = tmp_path / "programdata-initrd.img"
    staged.write_bytes(b"staged")
    monkeypatch.setattr(hcs_mod, "_DEFAULT_INITRD", str(staged))
    mine = tmp_path / "mine.img"
    mine.write_bytes(b"mine")
    monkeypatch.setenv("OPENSHRIMP_HCS_INITRD", str(mine))

    assert hcs_mod.ensure_initrd() == mine
    assert asked == []


def test_nothing_staged_downloads_into_the_cache(
    monkeypatch, tmp_path, asked, _cache_dir,
):
    monkeypatch.setattr(hcs_mod, "_DEFAULT_INITRD", str(tmp_path / "absent"))

    got = hcs_mod.ensure_initrd()

    assert got == _cache_dir / "initrd.img"
    assert asked == [(hcs_mod.INITRD_ASSET, str(_cache_dir / "initrd.img"))]


def test_an_override_pointing_at_nothing_does_not_download(
    monkeypatch, tmp_path, asked,
):
    """Naming a path is a statement about which initramfs to boot; silently
    fetching a different one would ignore it."""
    monkeypatch.setenv("OPENSHRIMP_HCS_INITRD", str(tmp_path / "absent"))

    with pytest.raises(RuntimeError, match="OPENSHRIMP_HCS_INITRD"):
        hcs_mod.ensure_initrd()
    assert asked == []


# -- the rootfs template ------------------------------------------------------


def test_no_base_image_downloads_the_released_rootfs(
    monkeypatch, tmp_path, asked, _cache_dir,
):
    sb = _sandbox(tmp_path, monkeypatch)

    template = sb._rootfs_template()

    assert template == _cache_dir / "base-rootfs.vhdx"
    assert asked == [
        (hcs_mod.BASE_ROOTFS_ASSET, str(_cache_dir / "base-rootfs.vhdx")),
    ]


def test_computer_use_downloads_only_the_desktop_image(
    monkeypatch, tmp_path, asked, _cache_dir,
):
    """The published desktop image is built from the published base, so it
    already carries the userland — fetching both would double a multi-gigabyte
    download for nothing."""
    sb = _sandbox(tmp_path, monkeypatch, computer_use=True)

    template = sb._rootfs_template()

    assert template == _cache_dir / "gui-rootfs.vhdx"
    assert asked == [
        (hcs_mod.GUI_ROOTFS_ASSET, str(_cache_dir / "gui-rootfs.vhdx")),
    ]


def test_a_configured_base_image_is_never_downloaded(
    monkeypatch, tmp_path, asked,
):
    base = tmp_path / "my-root.vhdx"
    base.write_bytes(b"")
    sb = _sandbox(tmp_path, monkeypatch, base_image=str(base))

    assert sb._rootfs_template() == base
    assert asked == []


def test_a_configured_base_image_that_is_absent_is_an_error(
    monkeypatch, tmp_path, asked,
):
    sb = _sandbox(tmp_path, monkeypatch, base_image=str(tmp_path / "absent.vhdx"))

    with pytest.raises(RuntimeError, match="base_image not found"):
        sb._rootfs_template()
    assert asked == []


def test_computer_use_on_a_configured_base_image_wants_the_baked_variant(
    monkeypatch, tmp_path, asked,
):
    """An operator supplying their own userland has to bake the desktop
    variant from it; the released image would not be their guest."""
    base = tmp_path / "my-root.vhdx"
    base.write_bytes(b"")
    sb = _sandbox(
        tmp_path, monkeypatch, base_image=str(base), computer_use=True,
    )

    with pytest.raises(RuntimeError, match="build_hcs_gui_rootfs.sh"):
        sb._rootfs_template()
    assert asked == []
