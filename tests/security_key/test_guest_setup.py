from __future__ import annotations

from open_shrimp.security_key.guest_setup import (
    UDEV_RULE,
    UDEV_RULE_PATH,
    install_udev_rule_cmd,
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
