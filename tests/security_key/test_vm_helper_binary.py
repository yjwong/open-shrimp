from __future__ import annotations

from pathlib import Path

import pytest

from open_shrimp.security_key.vm_helper_binary import (
    BINARY_NAME,
    asset_name_for_linux_arch,
    download_url_for_linux_arch,
    install_cmd_for_linux_guest,
)


def test_asset_name_for_linux_arch() -> None:
    assert asset_name_for_linux_arch("x86_64") == f"{BINARY_NAME}-linux-x86_64"
    assert asset_name_for_linux_arch("amd64") == f"{BINARY_NAME}-linux-x86_64"
    assert asset_name_for_linux_arch("x64") == f"{BINARY_NAME}-linux-x86_64"
    assert asset_name_for_linux_arch("aarch64") == f"{BINARY_NAME}-linux-aarch64"
    assert asset_name_for_linux_arch("arm64") == f"{BINARY_NAME}-linux-aarch64"


def test_asset_name_rejects_unsupported_arch() -> None:
    with pytest.raises(RuntimeError, match="Unsupported Linux architecture"):
        asset_name_for_linux_arch("riscv64")


def test_download_url_uses_release_asset_name() -> None:
    assert download_url_for_linux_arch("x86_64").endswith(
        f"/releases/latest/download/{BINARY_NAME}-linux-x86_64"
    )


def test_install_cmd_installs_helper_to_usr_local_bin() -> None:
    cmd = install_cmd_for_linux_guest("https://example.invalid/helper")
    assert "curl -fsSL https://example.invalid/helper" in cmd
    assert f"sudo install -m 755 /tmp/{BINARY_NAME}" in cmd
    assert f"/usr/local/bin/{BINARY_NAME}" in cmd


def test_release_workflow_builds_expected_helper_assets() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yaml"
    ).read_text(encoding="utf-8")
    assert asset_name_for_linux_arch("x86_64") in workflow
    assert asset_name_for_linux_arch("aarch64") in workflow
