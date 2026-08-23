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

import logging
import os
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from open_shrimp.binaries import find_binary, managed_binary
from open_shrimp.config import (
    _SANDBOX_BACKENDS,
    Config,
    SandboxConfig,
    load_config,
)
from open_shrimp.paths import init_paths

logger = logging.getLogger(__name__)


# Both downloads are reported on the managed copy alone, because that is the
# only copy they will ever run — a build on $PATH reads as absent here rather
# than passing the check on a binary that is never reached.  Neither absence
# is a fault to fix: the download happens on first use.


def _check_moonshine_stt(config: Config | None) -> tuple[bool, str]:
    """The speech-to-text binary, fetched on first transcription.

    A check must not fetch anything, so an absent binary passes on the
    strength of that download — the same rule the sandbox assets follow.
    Only a platform no build is published for fails, because there the
    download has nothing to fetch.
    """
    from open_shrimp.stt import moonshine_stt_downloadable

    path = managed_binary("moonshine-stt")
    if path:
        return True, f"found at {path}"
    if not moonshine_stt_downloadable():
        return False, (
            f"no {platform.system()} {platform.machine()} build is published "
            "— build it from the moonshine-stt/ directory in the repository"
        )
    return True, "not downloaded yet — fetched on first transcription"


def _check_cloudflared(config: Config | None) -> tuple[bool, str]:
    """The tunnel binary, fetched on first tunnel start.

    Passes when absent for the same reason ``_check_moonshine_stt`` does: the
    download that will supply it is the answer, and a check may not run it.
    """
    from open_shrimp.tunnel import cloudflared_downloadable

    path = managed_binary("cloudflared")
    if path:
        return True, f"found at {path}"
    if not cloudflared_downloadable():
        return False, (
            f"no {platform.system()} {platform.machine()} build is published "
            "— install cloudflared yourself and put it on $PATH"
        )
    return True, "not downloaded yet — fetched on first tunnel start"


def _check_docker(config: Config | None) -> tuple[bool, str]:
    path = shutil.which("docker")
    if not path:
        return False, (
            "docker CLI not found — install Docker Engine (or Docker "
            "Desktop) and make sure docker is on your PATH"
        )
    # Check if daemon is responsive.
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, _docker_refusal(result.stderr)
    except subprocess.TimeoutExpired:
        return False, (
            "docker CLI found but the daemon did not answer in 10 seconds — "
            "it is starting up or wedged; restart it with: sudo systemctl "
            "restart docker  (or restart Docker Desktop)"
        )
    except Exception as e:
        return False, f"docker CLI found but check failed: {e}"
    return True, f"found at {path}, daemon running"


def _docker_refusal(stderr: bytes) -> str:
    """Why ``docker info`` failed, told apart by what the daemon said.

    A socket that refuses this account and a daemon that is not there both
    exit non-zero, and the remedies are opposites: one is a group membership,
    the other is starting a service.  Conflating them sends an operator to
    start a daemon that is already running, so the permission case is split
    out on the only evidence there is — the CLI's own words.
    """
    said = stderr.decode("utf-8", "replace").lower()
    if "permission denied" in said:
        return (
            "docker CLI found, but this account is not allowed to talk to the "
            "Docker daemon — the daemon may well be running; add yourself to "
            "the docker group with: sudo usermod -aG docker $USER  — then log "
            "out and back in, because the group is read from the token your "
            "session was created with, so a new terminal in the same session "
            "still will not have it"
        )
    return (
        "docker CLI found but daemon is not running — start it with: sudo "
        "systemctl start docker  (or launch Docker Desktop)"
    )


def _check_libvirt(config: Config | None) -> tuple[bool, str]:
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


def _check_virtiofsd(config: Config | None) -> tuple[bool, str]:
    """The filesystem daemon the VMs share the project through.

    An absent one is not a problem to report: the manager downloads a patched
    build on first use, so this passes on the strength of that download the
    same way the HCS control initramfs check does.  Only the version-gated
    system binaries it would fall back to are inspected here, because a check
    must not fetch anything.
    """
    # Re-use the same logic from libvirt_helpers.
    from open_shrimp.sandbox.libvirt_helpers import find_virtiofsd

    path = find_virtiofsd()
    if path:
        return True, f"found at {path}"
    return True, (
        "not staged — a patched build will be downloaded on first use"
    )


def _check_lima(config: Config | None) -> tuple[bool, str]:
    """``limactl``, which the backend downloads when the host has none.

    A check must not fetch anything, so an absent ``limactl`` passes on the
    strength of that download — the same rule the HCS assets follow.  Only a
    platform Lima publishes no build for fails, because there the download
    has nothing to fetch and Homebrew is the one remaining route.

    Not ``find_binary``: that is for programs this project does not manage,
    and a ``limactl`` already downloaded into the managed bin directory is on
    nobody's ``$PATH``.
    """
    from open_shrimp.sandbox.lima_helpers import (
        LIMA_VERSION,
        find_limactl,
        limactl_downloadable,
    )

    path = find_limactl()
    if path:
        return True, f"found at {path}"
    if not limactl_downloadable():
        return False, (
            f"not found, and Lima publishes no {platform.system()} "
            f"{platform.machine()} build to download — install it with: "
            "brew install lima"
        )
    return True, (
        f"not installed — Lima {LIMA_VERSION} will be downloaded on first use"
    )


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


def _check_hcs_rdp_helper(config: Config | None) -> tuple[bool, str]:
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


# Each check: (label, function, platform filter or None for all, the sandbox
# backends it is a prerequisite of).  The backend tag lives here rather than in
# a table beside it so the two cannot come to name different things.
_Check = Callable[[Config | None], tuple[bool, str]]
_CHECKS: list[tuple[str, _Check, str | None, tuple[str, ...]]] = [
    ("moonshine-stt", _check_moonshine_stt, None, ()),
    ("cloudflared", _check_cloudflared, None, ()),
    ("Docker", _check_docker, "Linux", ("docker",)),
    ("libvirt", _check_libvirt, "Linux", ("libvirt",)),
    ("virtiofsd", _check_virtiofsd, "Linux", ("libvirt",)),
    ("Lima", _check_lima, "Darwin", ("lima",)),
    ("win32more", _check_win32more, "Windows", ("hcs",)),
    ("Hyper-V rights", _check_hyperv_rights, "Windows", ("hcs",)),
    ("HCS kernel", _check_hcs_kernel, "Windows", ("hcs",)),
    ("HCS control initramfs", _check_hcs_initrd, "Windows", ("hcs",)),
    ("csc", _check_csc, "Windows", ("hcs",)),
    ("HCS base image", _check_hcs_base_image, "Windows", ("hcs",)),
    ("HCS RDP helper", _check_hcs_rdp_helper, "Windows", ("hcs",)),
]


def checks_for_backend(backend: str) -> list[tuple[str, _Check]]:
    """``(label, check)`` for every prerequisite *backend* needs on this host.

    The remedies these return are the ones an operator should see wherever a
    sandbox refuses to start, not only under ``openshrimp doctor`` — so the
    platform filter is applied exactly as the report applies it, and the two
    cannot disagree about which checks even ran.  A host where none of them
    apply gets an empty list, which is a caller's cue to say nothing rather
    than to report a pass nobody established.
    """
    current = platform.system()
    return [
        (label, fn)
        for label, fn, plat, backends in _CHECKS
        if backend in backends and (plat is None or plat == current)
    ]


@dataclass(frozen=True)
class Outcome:
    """What one prerequisite answered.

    ``ok`` is ``None`` where the check could not answer at all.  A probe that
    crashed is neither a pass nor a failure, and every surface that renders
    these — the boot card, the report, the reply to a sandbox that would not
    start — has to tell those three apart, so the distinction is made once
    here rather than three times in the rendering.
    """

    label: str
    ok: bool | None
    detail: str


def run_check(label: str, check: _Check, config: Config | None) -> Outcome:
    """*check*'s answer, or the fact that it had none."""
    try:
        ok, detail = check(config)
    except Exception as e:
        logger.warning("The %s check failed to run", label, exc_info=True)
        return Outcome(label, None, f"this check could not run: {e!r}")
    return Outcome(label, ok, detail)


def prerequisites(backend: str, config: Config | None) -> list[Outcome]:
    """Every prerequisite *backend* needs here, run against this host now."""
    return [
        run_check(label, check, config)
        for label, check in checks_for_backend(backend)
    ]


@dataclass(frozen=True)
class SandboxOffer:
    """One isolation choice a first config may be given, and whether it works.

    ``detail`` carries the remedy when ``available`` is false, so a wizard can
    say why a backend it names is not on offer instead of leaving a gap the
    user reads as a missing feature.
    """

    backend: str
    label: str
    summary: str
    available: bool
    detail: str


# What each backend is, in the words a person choosing one needs — not the
# words an operator debugging one needs.  Beside the offer rather than in the
# three wizards, so the answer to "what does this do to my project" is the
# same sentence on every platform.
#
# Which backends exist is not decided here: the offer list is driven off the
# registry the config validates against, so a backend added there and not
# given words is a KeyError a test catches rather than a choice the wizard
# silently never shows.
_SANDBOX_LABELS: dict[str, tuple[str, str]] = {
    "docker": ("Docker", "each project runs in a container on this computer"),
    "libvirt": ("libvirt", "each project runs in its own virtual machine"),
    "lima": ("Lima", "each project runs in its own Linux virtual machine"),
    "hcs": ("Hyper-V", "each project runs in its own Linux virtual machine"),
}


def _offer(backend: str, config: Config | None) -> SandboxOffer | None:
    """What *backend* would give a context here, or ``None`` if it cannot run.

    A backend with no prerequisite that applies here cannot run here at all —
    ``checks_for_backend`` already filters by platform — so it answers
    ``None`` rather than being offered and then refused at the first turn.
    One that does apply is probed, because a backend whose daemon is not
    running would otherwise be written into the config and fail every turn
    from then on, far from the wizard that could still have said so.
    """
    # One expression of the platform filter, not two: the empty list *is* the
    # "nothing here applies" answer, so asking `checks_for_backend` separately
    # would be a second reading of it that can disagree.
    outcomes = prerequisites(backend, config)
    if not outcomes:
        return None
    label, summary = _SANDBOX_LABELS[backend]
    failed = [o for o in outcomes if not o.ok]
    # The check's own name is dropped where it repeats the backend's, so a
    # single-prerequisite backend reads as "Docker — docker CLI not found"
    # rather than naming Docker twice in one line.
    return SandboxOffer(
        backend=backend,
        label=label,
        summary=summary,
        available=not failed,
        detail="; ".join(
            o.detail if o.label == label else f"{o.label}: {o.detail}"
            for o in failed
        ),
    )


def sandbox_offers(config: Config | None = None) -> list[SandboxOffer]:
    """Every sandbox backend a context on this host could be given.

    The whole catalog, for somebody choosing between its entries — which is
    the ``sandboxes`` table and nothing else today.  Setup does not use it:
    see ``blessed_offer``, because a first config asks whether a project is
    isolated, not which hypervisor isolates it.
    """
    offers = (_offer(backend, config) for backend in sorted(_SANDBOX_BACKENDS))
    return [offer for offer in offers if offer is not None]


# One backend per platform, and setup names no other.  Somebody who has to be
# told what libvirt is cannot weigh it against Docker, so offering the choice
# buys nothing and costs the one question of the step its answer; the pick
# here is the best-trodden path on each platform, and the config Mini App can
# still move a context to any backend afterwards.
_BLESSED_BACKEND: dict[str, str] = {
    "Linux": "libvirt",
    "Darwin": "lima",
    "Windows": "hcs",
}

# What "Enable sandbox" promises, in the words of somebody who has never heard
# of a hypervisor.  Beside the backends rather than in the three wizards, so
# the promise is the same sentence on every platform.
#
# Outcome only, no mechanism.  "Virtual machine" is the same category of word
# as libvirt and Hyper-V — the category this question exists to stop showing
# people — and explaining "sandbox" with it explains one unfamiliar word using
# another.  What the mechanism was really carrying is the *cost*, so that is
# said directly: a first turn that quietly downloads an image reads as a hang
# unless something warned it would take a while.
#
# The off state and the price are named here rather than left to each wizard,
# because a caption that sells only the upside is not the choice being made.
# Both figures are ceilings a context may raise, not amounts taken up front:
# the memory ceiling returns unused pages to the host through free-page
# reporting, and the disk is a sparse overlay.  "Up to" is what makes them
# true — the numbers themselves come from SandboxConfig's defaults, which is
# also why they are not repeated as literals here.
_SANDBOX_DEFAULTS = SandboxConfig(backend="libvirt")
SANDBOX_SUMMARY = (
    "Anything you send me in Telegram can reach that project's folder, and "
    "nothing else on this computer. Turn this off and it can reach "
    "everything. Projects take a few minutes to start the first time, and "
    f"use up to {_SANDBOX_DEFAULTS.memory // 1024} GB of memory and "
    f"{_SANDBOX_DEFAULTS.disk_size} GB of disk."
)


def blessed_backend() -> str | None:
    """The one sandbox backend setup offers on this platform."""
    return _BLESSED_BACKEND.get(platform.system())


def blessed_offer(config: Config | None = None) -> SandboxOffer | None:
    """What enabling the sandbox would mean here, and whether it can be honoured.

    ``None`` where this platform has no blessed backend at all; an offer with
    ``available`` false where it has one whose prerequisites are missing.
    ``sandbox_note`` is what a front end should say about either.
    """
    backend = blessed_backend()
    return _offer(backend, config) if backend is not None else None


def sandbox_note(offer: SandboxOffer | None) -> str:
    """The sentence beside the toggle: what it gets you, or why you cannot have it.

    All three cases answered here rather than in each of the three wizards.
    Composing this per front end is how they came to disagree about the one
    that matters — a host with nothing to offer said nothing at all in two of
    them, which is the "reads as a missing feature" failure the remedy exists
    to prevent.

    "This computer" on every platform, rather than Mac and PC and computer.
    The word costs nothing to a reader and is the last thing that would have
    to be said three times.
    """
    if offer is None:
        return (
            "There is no sandbox I can set up on this computer, so your "
            "projects will run directly on it."
        )
    if not offer.available:
        return (
            f"{offer.label} is unavailable: {offer.detail}. Your projects will "
            "run directly on this computer."
        )
    return SANDBOX_SUMMARY


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
    """Run all checks and print results. Returns 0 if all pass, 1 otherwise.

    The instance paths are initialised first because several checks look for
    managed binaries under them — in the bot the core does it at startup, and
    a report that dies partway through is the one thing a diagnostic may not
    do.  One check's crash is likewise not the report's: the rest still run,
    and the failure is printed as itself rather than swallowed into a pass.
    """
    current_platform = platform.system()
    config = _load_config(config_path)
    init_paths(config.instance_name if config is not None else None)
    _use_utf8_output()
    pass_icon, fail_icon = _icons()
    has_failure = False

    for label, check_fn, plat, _backends in _CHECKS:
        if plat is not None and current_platform != plat:
            continue
        outcome = run_check(label, check_fn, config)
        icon = pass_icon if outcome.ok else fail_icon
        print(_printable(f"  {icon} {label}: {outcome.detail}"))
        if not outcome.ok:
            has_failure = True

    return 1 if has_failure else 0
