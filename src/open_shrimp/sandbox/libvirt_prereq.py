"""Prerequisites declared by the libvirt sandbox backend."""

from __future__ import annotations

from typing import Any

from open_shrimp.sandbox.prerequisites import (
    COMMON_CONFIG_FIELDS,
    SandboxBackendDeclaration,
)


def _check_libvirt(config: Any) -> tuple[bool, str]:
    from open_shrimp.sandbox.libvirt_helpers import LIBVIRT_INSTALL_REMEDY

    try:
        import libvirt  # type: ignore[import-untyped]
    except ImportError:
        return False, f"libvirt-python not installed — {LIBVIRT_INSTALL_REMEDY}"

    try:
        conn = libvirt.open("qemu:///session")
        conn.close()
    except Exception as e:
        return False, (
            f"libvirt-python installed but cannot connect: {e} — install "
            "the daemon with: sudo apt install libvirt-daemon qemu-system-x86"
        )
    return True, "libvirt-python installed, qemu:///session reachable"


def _check_virtiofsd(config: Any) -> tuple[bool, str]:
    """The filesystem daemon the VMs share the project through.

    An absent one is not a problem to report: the manager downloads a patched
    build on first use, so this passes on the strength of that download the
    same way the HCS control initramfs check does.  Only the version-gated
    system binaries it would fall back to are inspected here, because a check
    must not fetch anything.
    """
    from open_shrimp.sandbox.libvirt_helpers import find_virtiofsd

    path = find_virtiofsd()
    if path:
        return True, f"found at {path}"
    return True, (
        "not staged — a patched build will be downloaded on first use"
    )


DECLARATION = SandboxBackendDeclaration(
    backend="libvirt",
    label="libvirt",
    summary="each project runs in its own virtual machine",
    platform="Linux",
    checks=(
        ("libvirt", _check_libvirt),
        ("virtiofsd", _check_virtiofsd),
    ),
    capabilities=COMMON_CONFIG_FIELDS | {
        "virgl", "phone_use", "android", "persistent_paths",
    },
    base_image_placeholder="Path to base qcow2/cloud image",
    unsupported_reasons=((
        "mingw_bin",
        "mingw_bin applies only to the hcs backend, where it builds the "
        "Windows RDP helper",
    ),),
)
