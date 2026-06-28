from __future__ import annotations

from open_shrimp.security_key.guest_setup import (
    UDEV_RULE,
    UDEV_RULE_PATH,
    install_udev_rule_cmd,
    provision_uhid_cmd,
    setup_security_key_guest_cmd,
)


def test_install_udev_rule_cmd_installs_virtual_fido_hidraw_rule() -> None:
    cmd = install_udev_rule_cmd()
    assert 'KERNELS=="0003:1209:F1D0.*"' in UDEV_RULE
    assert 'GROUP="input"' in UDEV_RULE
    assert 'MODE="0660"' in UDEV_RULE
    assert UDEV_RULE_PATH in cmd
    assert "udevadm control --reload-rules" in cmd
    assert "udevadm trigger --subsystem-match=hidraw" in cmd


def test_install_udev_rule_cmd_only_tolerates_trigger_failure() -> None:
    cmd = install_udev_rule_cmd()
    assert "&& (sudo udevadm trigger --subsystem-match=hidraw || true)" in cmd


def test_provision_uhid_cmd_installs_kernel_extra_modules() -> None:
    cmd = provision_uhid_cmd()
    assert "sudo modprobe uhid" in cmd
    assert "linux-modules-extra-$(uname -r)" in cmd
    assert "sudo apt-get install -y" in cmd
    assert "test -e /dev/uhid" in cmd
    assert "then exit 0" not in cmd


def test_setup_security_key_guest_cmd_provisions_uhid_and_udev() -> None:
    cmd = setup_security_key_guest_cmd()
    assert "sudo modprobe uhid" in cmd
    assert UDEV_RULE_PATH in cmd
    assert "udevadm control --reload-rules" in cmd
