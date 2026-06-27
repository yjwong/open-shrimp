"""Guest setup commands for security-key forwarding."""

from __future__ import annotations

import shlex

UDEV_RULE_PATH = "/etc/udev/rules.d/70-openshrimp-security-key.rules"
UDEV_RULE = (
    'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", '
    'KERNELS=="0003:1209:F1D0.*", GROUP="input", MODE="0660"'
)


def install_udev_rule_cmd() -> str:
    """Return a shell command that installs the virtual FIDO hidraw udev rule."""
    quoted_rule = shlex.quote(UDEV_RULE + "\n")
    quoted_path = shlex.quote(UDEV_RULE_PATH)
    return (
        f"printf %s {quoted_rule} | sudo tee {quoted_path} >/dev/null "
        "&& sudo udevadm control --reload-rules "
        "&& (sudo udevadm trigger --subsystem-match=hidraw || true)"
    )
