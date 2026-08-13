"""Detect common issues with optional components.

Run via ``openshrimp doctor`` to check that optional dependencies
(moonshine-stt, cloudflared, Docker, libvirt, virtiofsd, Lima, etc.)
are available and functional.

Every check takes the loaded config (``None`` when there is none to load) and
returns ``(ok, detail)``.  Checks that probe a host-wide fact ignore it; the
HCS ones that are per-context — the base rootfs image and the computer-use
RDP helper — read it, because those facts live in ``SandboxConfig`` and there
is nothing to check on a host that configures no HCS context.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from open_shrimp.binaries import find_binary
from open_shrimp.config import Config, SandboxConfig, load_config


def _check_moonshine_stt(config: Config | None) -> tuple[bool, str]:
    path = find_binary("moonshine-stt")
    if path:
        return True, f"found at {path}"
    return False, "not found (voice transcription unavailable)"


def _check_cloudflared(config: Config | None) -> tuple[bool, str]:
    path = find_binary("cloudflared")
    if path:
        return True, f"found at {path}"
    return False, "not found (tunnel support unavailable)"


def _check_docker(config: Config | None) -> tuple[bool, str]:
    path = shutil.which("docker")
    if not path:
        return False, "docker CLI not found"
    # Check if daemon is responsive.
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "docker CLI found but daemon is not running"
    except subprocess.TimeoutExpired:
        return False, "docker CLI found but daemon timed out"
    except Exception as e:
        return False, f"docker CLI found but check failed: {e}"
    return True, f"found at {path}, daemon running"


def _check_libvirt(config: Config | None) -> tuple[bool, str]:
    try:
        import libvirt  # type: ignore[import-untyped]
    except ImportError:
        return False, "libvirt-python not installed"

    try:
        conn = libvirt.open("qemu:///session")
        conn.close()
    except Exception as e:
        return False, f"libvirt-python installed but cannot connect: {e}"
    return True, "libvirt-python installed, qemu:///session reachable"


def _check_virtiofsd(config: Config | None) -> tuple[bool, str]:
    # Re-use the same logic from libvirt_helpers.
    from open_shrimp.sandbox.libvirt_helpers import find_virtiofsd

    path = find_virtiofsd()
    if path:
        return True, f"found at {path}"
    return False, "not found (required for libvirt sandbox)"


def _check_lima(config: Config | None) -> tuple[bool, str]:
    path = _find_managed_or_path("limactl")
    if path:
        return True, f"found at {path}"
    return False, "not found (required for Lima sandbox on macOS)"


# -- HCS (Windows) ------------------------------------------------------------
#
# Every remedy named here is the one ``hcs.ensure_environment`` names when the
# same thing is missing at boot, so an operator gets one story either way.


def _hcs_sandboxes(config: Config | None) -> list[tuple[str, SandboxConfig]]:
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


def _no_hcs_context(config: Config | None) -> str:
    """Why there is nothing per-context to check."""
    if config is None:
        return "no config loaded (nothing to check)"
    return "no context uses the hcs backend (nothing to check)"


def _check_win32more(config: Config | None) -> tuple[bool, str]:
    try:
        import win32more  # noqa: F401
    except ImportError:
        return False, (
            "not installed (required for the HCS sandbox) \u2014 install the 'hcs' "
            "extra: uv sync --extra hcs"
        )
    return True, "installed"


def _check_hyperv_rights(config: Config | None) -> tuple[bool, str]:
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


def _check_hcs_kernel(config: Config | None) -> tuple[bool, str]:
    from open_shrimp.sandbox.hcs import kernel_path

    path = kernel_path()
    if path.exists():
        return True, f"found at {path}"
    return False, (
        f"not found at {path} \u2014 install WSL (the kernel ships with it) or set "
        "OPENSHRIMP_HCS_KERNEL (required for HCS sandbox)"
    )


def _check_hcs_initrd(config: Config | None) -> tuple[bool, str]:
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


def _check_csc(config: Config | None) -> tuple[bool, str]:
    from open_shrimp.sandbox.hcs import find_csc

    try:
        return True, f"found at {find_csc()}"
    except RuntimeError as e:
        return False, f"{str(e).rstrip('.')} (required to build the HCS launcher)"


def _check_hcs_base_image(config: Config | None) -> tuple[bool, str]:
    """The per-context rootfs template, plus its baked ``-gui`` variant where
    the context enables computer use.

    A context with no ``base_image`` boots the released rootfs, so it is
    reported as a pending download rather than a problem; only an operator who
    named their own image can be missing one.
    """
    from open_shrimp.sandbox.hcs import BASE_ROOTFS_ASSET, GUI_ROOTFS_ASSET
    from open_shrimp.sandbox.hcs_assets import asset_dir
    from open_shrimp.sandbox.hcs_helpers import gui_image_path

    sandboxes = _hcs_sandboxes(config)
    if not sandboxes:
        return True, _no_hcs_context(config)

    problems: list[str] = []
    found: list[str] = []
    for name, sandbox in sandboxes:
        if not sandbox.base_image:
            gui = sandbox.computer_use
            asset = GUI_ROOTFS_ASSET if gui else BASE_ROOTFS_ASSET
            cached = asset_dir() / ("gui-rootfs.vhdx" if gui else "base-rootfs.vhdx")
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


def _check_hcs_rdp_helper(config: Config | None) -> tuple[bool, str]:
    """The computer-use RDP helper and the FreeRDP DLLs it loads.

    Only the prebuilt bundle and the local toolchain are inspected \u2014 never the
    download that ``ensure_rdp_helper`` would fall back to, since a check must
    not fetch anything.  Either source is enough, so a host with a toolchain
    and no bundle passes.
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

    # No bundle: the source build is the remaining path, and it needs both a
    # toolchain and the FreeRDP DLLs from the same mingw64 bin directory.
    problems: list[str] = []
    buildable: list[str] = []
    for name, sandbox in sandboxes:
        if not sandbox.mingw_bin:
            problems.append(
                f"{name}: no prebuilt helper in {shipped_helper_dir()} and no "
                "sandbox.mingw_bin to build one \u2014 unpack the "
                f"{HELPER_ASSET} release asset there (or point "
                "OPENSHRIMP_HCS_RDP_HELPER at it), or set sandbox.mingw_bin to "
                r"an MSYS2 mingw64 bin directory (e.g. C:\msys64\mingw64\bin)"
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
    return True, "no prebuilt helper; " + "; ".join(buildable)


def _missing_freerdp_dlls(directory: Path) -> list[str]:
    """Which of the FreeRDP libraries the helper links are absent from
    *directory*.  Matched by prefix \u2014 the soname carries a version the bundle
    inherits from whatever FreeRDP 3 build produced it."""
    return [
        f"{stem}*.dll"
        for stem in ("libfreerdp-client", "libfreerdp", "libwinpr")
        if not any(directory.glob(f"{stem}*.dll"))
    ]


# Each check: (label, function, platform filter or None for all).
_CHECKS: list[tuple[str, Callable[[Config | None], tuple[bool, str]], str | None]] = [
    ("moonshine-stt", _check_moonshine_stt, None),
    ("cloudflared", _check_cloudflared, None),
    ("Docker", _check_docker, "Linux"),
    ("libvirt", _check_libvirt, "Linux"),
    ("virtiofsd", _check_virtiofsd, "Linux"),
    ("Lima", _check_lima, "Darwin"),
    ("win32more", _check_win32more, "Windows"),
    ("Hyper-V rights", _check_hyperv_rights, "Windows"),
    ("HCS kernel", _check_hcs_kernel, "Windows"),
    ("HCS control initramfs", _check_hcs_initrd, "Windows"),
    ("csc", _check_csc, "Windows"),
    ("HCS base image", _check_hcs_base_image, "Windows"),
    ("HCS RDP helper", _check_hcs_rdp_helper, "Windows"),
]


def _load_config(path: str | None) -> Config | None:
    """The operator's config, or ``None`` when there is none to read.

    A host being checked may have no config yet, or one that no longer parses;
    neither is a reason to skip the host-wide checks, so the per-context ones
    simply have nothing to look at.
    """
    try:
        return load_config(path)
    except Exception:
        return None


def _use_utf8_output() -> None:
    """Put the report on a UTF-8 stream wherever the stream allows it.

    A Windows console is UTF-8 already, but a redirected or piped stdout takes
    the locale codepage — cp1252 on an English install, which *raises* on a
    character outside it instead of substituting one.  Re-encoding as UTF-8 is
    both what any consumer of the piped output expects and what keeps the
    report from dying partway through.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (OSError, ValueError):
        pass


def _printable(text: str) -> str:
    """*text* with whatever the output stream cannot encode replaced.

    The fallback for a stream that would not take UTF-8: a report carrying a
    placeholder beats one that raises in the middle of printing.
    """
    encoding = sys.stdout.encoding or "ascii"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:
        return text.encode("ascii", "replace").decode("ascii")
    return text


def _icons() -> tuple[str, str]:
    """The pass/fail markers, ASCII wherever the emoji ones do not survive
    the output encoding."""
    if _printable("\u2705\u274c") == "\u2705\u274c":
        return "\u2705", "\u274c"
    return "[ok]", "[!!]"


def run_doctor(config_path: str | None = None) -> int:
    """Run all checks and print results. Returns 0 if all pass, 1 otherwise."""
    current_platform = platform.system()
    config = _load_config(config_path)
    _use_utf8_output()
    pass_icon, fail_icon = _icons()
    has_failure = False

    for label, check_fn, plat in _CHECKS:
        if plat is not None and current_platform != plat:
            continue
        ok, detail = check_fn(config)
        icon = pass_icon if ok else fail_icon
        print(_printable(f"  {icon} {label}: {detail}"))
        if not ok:
            has_failure = True

    return 1 if has_failure else 0
