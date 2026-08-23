"""Prerequisites declared by the HCS sandbox backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from open_shrimp.sandbox.prerequisites import (
    COMMON_CONFIG_FIELDS,
    SandboxBackendDeclaration,
)


def _hcs_sandboxes(config: Any) -> list[tuple[str, Any]]:
    """``(context name, sandbox)`` for every context on the HCS backend."""
    if config is None:
        return []
    return [
        (name, ctx.sandbox)
        for name, ctx in config.contexts.items()
        if ctx.sandbox is not None
        and ctx.sandbox.enabled
        and ctx.sandbox.backend == "hcs"
    ]


def _no_hcs_context(config: Any) -> str:
    """Why there is nothing per-context to check."""
    if config is None:
        return "no config loaded (nothing to check)"
    return "no context uses the hcs backend (nothing to check)"


def _check_win32more(config: Any) -> tuple[bool, str]:
    try:
        import win32more  # noqa: F401
    except ImportError:
        return False, (
            "not installed (required for the HCS sandbox) \u2014 install the 'hcs' "
            "extra: uv sync --extra hcs"
        )
    return True, "installed"


def _check_hyperv_rights(config: Any) -> tuple[bool, str]:
    """Whether this token may drive HCS at all.

    A caller in neither Administrators nor Hyper-V Administrators is refused
    by ``HcsCreateComputeSystem`` with a bare ``0x8037011B``, and membership is
    of the *effective* token \u2014 so an unelevated process whose account is
    privileged reads as unprivileged here, exactly as it will at create time.
    """
    try:
        from open_shrimp.sandbox import hcs_win as W
    except ImportError as e:
        return False, f"cannot probe (win32more unavailable: {e})"

    try:
        permitted = W.can_manage_compute_systems()
    except OSError as e:
        # A probe that cannot answer is not an answer of "no": the sandbox
        # still gets its chance to boot.
        return True, f"could not be probed ({e}) \u2014 HCS will decide at create"
    if permitted:
        return True, "this token is in Administrators or Hyper-V Administrators"
    return False, (
        "this token is in neither Administrators nor Hyper-V Administrators \u2014 "
        "HCS refuses to create a compute system (0x8037011B); add the account "
        "to Hyper-V Administrators and sign out and back in, or run elevated"
    )


def _check_hcs_kernel(config: Any) -> tuple[bool, str]:
    from open_shrimp.sandbox.hcs import kernel_path

    path = kernel_path()
    if path.exists():
        return True, f"found at {path}"
    return False, (
        f"not found at {path} \u2014 install WSL (the kernel ships with it) or set "
        "OPENSHRIMP_HCS_KERNEL (required for HCS sandbox)"
    )


def _check_hcs_initrd(config: Any) -> tuple[bool, str]:
    """The control initramfs, which the backend downloads when none is staged.

    A check must not fetch anything, so an absent initramfs passes on the
    strength of that download.  Only ``OPENSHRIMP_HCS_INITRD`` pointing at
    nothing fails: it suppresses the download, leaving no path to an image.
    """
    from open_shrimp.sandbox.hcs import INITRD_ASSET, initrd_path

    path = initrd_path()
    if path.exists():
        return True, f"found at {path}"
    if os.environ.get("OPENSHRIMP_HCS_INITRD"):
        return False, (
            f"OPENSHRIMP_HCS_INITRD points at {path}, which does not exist "
            "\u2014 unset it to download the released initramfs instead, or build "
            "one with scripts/build_hcs_initrd.sh (run as root in WSL)"
        )
    return True, (
        f"not staged \u2014 the {INITRD_ASSET} release asset will be downloaded to "
        f"{path} on first use"
    )


def _check_csc(config: Any) -> tuple[bool, str]:
    from open_shrimp.sandbox.hcs import find_csc

    try:
        return True, f"found at {find_csc()}"
    except RuntimeError as e:
        return False, f"{str(e).rstrip('.')} (required to build the HCS launcher)"


def _check_hcs_base_image(config: Any) -> tuple[bool, str]:
    """The per-context rootfs template, plus its baked ``-gui`` variant where
    the context enables computer use.

    A context with no ``base_image`` boots the released rootfs, so it is
    reported as a pending download rather than a problem; only an operator who
    named their own image can be missing one.
    """
    from open_shrimp.sandbox.hcs import managed_rootfs_asset
    from open_shrimp.sandbox.hcs_helpers import gui_image_path

    sandboxes = _hcs_sandboxes(config)
    if not sandboxes:
        return True, _no_hcs_context(config)

    problems: list[str] = []
    found: list[str] = []
    for name, sandbox in sandboxes:
        if not sandbox.base_image:
            asset, cached, _ = managed_rootfs_asset(
                computer_use=sandbox.computer_use,
            )
            if cached.exists():
                found.append(f"{name}: {cached} (downloaded)")
            else:
                found.append(
                    f"{name}: no sandbox.base_image — the {asset} release "
                    f"asset will be downloaded to {cached} on first use"
                )
            continue
        if not Path(sandbox.base_image).exists():
            problems.append(f"{name}: not found at {sandbox.base_image}")
            continue
        found.append(f"{name}: {sandbox.base_image}")
        if not sandbox.computer_use:
            continue
        gui = Path(gui_image_path(sandbox.base_image))
        if gui.exists():
            found.append(f"{name} (computer use): {gui}")
        else:
            problems.append(
                f"{name}: computer-use template not found at {gui} \u2014 bake it "
                "from the base image with scripts/build_hcs_gui_rootfs.sh "
                "(run as root inside WSL)"
            )

    if problems:
        return False, "; ".join(problems)
    return True, "; ".join(found)


def _check_hcs_rdp_helper(config: Any) -> tuple[bool, str]:
    """The computer-use RDP helper and the FreeRDP DLLs it loads.

    A check may not fetch anything, so what is inspected is the bundle already
    staged and the local toolchain.  With neither, this passes: the bundle is
    prefetched with the guest images and downloaded on first use failing that,
    so a host that has not staged one is missing nothing.  What fails is what
    no download repairs \u2014 an override resolving to nothing, a bundle without
    its DLLs, a ``mingw_bin`` without a compiler.
    """
    from open_shrimp.sandbox.hcs_rdp import (
        HELPER_ASSET,
        find_shipped_helper,
        shipped_helper_dir,
    )

    hcs = _hcs_sandboxes(config)
    sandboxes = [(name, sb) for name, sb in hcs if sb.computer_use]
    if not sandboxes:
        if hcs:
            return True, "no hcs context enables computer_use (nothing to check)"
        return True, _no_hcs_context(config)

    try:
        shipped = find_shipped_helper()
    except RuntimeError as e:
        return False, str(e)
    if shipped is not None:
        missing = _missing_freerdp_dlls(shipped.parent)
        if missing:
            return False, (
                f"found at {shipped}, but the FreeRDP DLLs it loads are not "
                f"beside it (no {', '.join(missing)} in {shipped.parent}) \u2014 "
                f"unpack the whole {HELPER_ASSET} bundle, not just the exe"
            )
        return True, f"found at {shipped}, FreeRDP DLLs alongside"

    # No bundle.  A configured toolchain is still inspected, since a source
    # install compiles from it and a half-installed MSYS2 fails that build.
    problems: list[str] = []
    pending: list[str] = []
    buildable: list[str] = []
    for name, sandbox in sandboxes:
        if not sandbox.mingw_bin:
            pending.append(
                f"{name}: the {HELPER_ASSET} release asset will be "
                f"downloaded to {shipped_helper_dir()} before it is needed"
            )
            continue
        mingw = Path(sandbox.mingw_bin)
        missing = [
            tool for tool in ("gcc.exe", "pkgconf.exe")
            if not (mingw / tool).is_file()
        ] + _missing_freerdp_dlls(mingw)
        if missing:
            problems.append(
                f"{name}: {mingw} is missing {', '.join(missing)} \u2014 install "
                "mingw-w64-x86_64-gcc, -pkgconf and -freerdp in MSYS2"
            )
            continue
        buildable.append(f"{name}: buildable from {mingw}")

    if problems:
        return False, "; ".join(problems)
    return True, "no prebuilt helper; " + "; ".join(pending + buildable)


def _missing_freerdp_dlls(directory: Path) -> list[str]:
    """Which of the FreeRDP libraries the helper links are absent from
    *directory*.  Matched by prefix \u2014 the soname carries a version the bundle
    inherits from whatever FreeRDP 3 build produced it."""
    return [
        f"{stem}*.dll"
        for stem in ("libfreerdp-client", "libfreerdp", "libwinpr")
        if not any(directory.glob(f"{stem}*.dll"))
    ]


DECLARATION = SandboxBackendDeclaration(
    backend="hcs",
    label="Hyper-V",
    summary="each project runs in its own Linux virtual machine",
    platform="Windows",
    checks=(
        ("win32more", _check_win32more),
        ("Hyper-V rights", _check_hyperv_rights),
        ("HCS kernel", _check_hcs_kernel),
        ("HCS control initramfs", _check_hcs_initrd),
        ("csc", _check_csc),
        ("HCS base image", _check_hcs_base_image),
        ("HCS RDP helper", _check_hcs_rdp_helper),
    ),
    capabilities=COMMON_CONFIG_FIELDS | {"persistent_paths", "mingw_bin"},
    base_image_placeholder="Path to rootfs VHDX (required)",
    unsupported_reasons=((
        "phone_use",
        "the WSL-shipped kernel is built without binder and uhid",
    ),),
)
