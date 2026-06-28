"""Guest setup commands for security-key forwarding."""

from __future__ import annotations

import shlex

UDEV_RULE_PATH = "/etc/udev/rules.d/70-openshrimp-security-key.rules"
UDEV_RULE = (
    'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", '
    'KERNELS=="0003:1209:F1D0.*", GROUP="input", MODE="0660"'
)


def provision_uhid_cmd() -> str:
    """Return a shell command that ensures Linux UHID support is available."""
    return " ".join(
        [
            "set -eu;",
            "if ! sudo modprobe uhid 2>/dev/null; then",
            "if command -v apt-get >/dev/null 2>&1; then",
            "export DEBIAN_FRONTEND=noninteractive;",
            "sudo apt-get update;",
            'sudo apt-get install -y "linux-modules-extra-$(uname -r)";',
            "sudo modprobe uhid;",
            "else",
            "echo 'uhid module is unavailable and apt-get is not installed' >&2;",
            "exit 1;",
            "fi;",
            "fi;",
            "test -e /dev/uhid",
        ]
    )


def setup_security_key_guest_cmd() -> str:
    """Return a shell command that prepares a Linux guest for forwarding."""
    return f"{provision_uhid_cmd()} && {install_udev_rule_cmd()}"


def install_udev_rule_cmd() -> str:
    """Return a shell command that installs the virtual FIDO hidraw udev rule."""
    quoted_rule = shlex.quote(UDEV_RULE + "\n")
    quoted_path = shlex.quote(UDEV_RULE_PATH)
    return (
        f"printf %s {quoted_rule} | sudo tee {quoted_path} >/dev/null "
        "&& sudo udevadm control --reload-rules "
        "&& (sudo udevadm trigger --subsystem-match=hidraw || true)"
    )
