"""Detect common issues with optional components.

Run via ``openshrimp doctor`` to check that optional dependencies
(moonshine-stt, cloudflared, libvirt, virtiofsd, Lima, etc.)
are available and functional.

Every check takes the loaded config (``None`` when there is none to load) and
returns ``(ok, detail)``.  Checks that probe a host-wide fact ignore it; the
HCS ones that are per-context — the base rootfs image and the computer-use
RDP helper — read it, because those facts live in ``SandboxConfig`` and there
is nothing to check on a host that configures no HCS context.
"""

from __future__ import annotations

import logging
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass

from open_shrimp.binaries import managed_binary
from open_shrimp.config import (
    Config,
    SandboxConfig,
    load_config,
)
from open_shrimp.paths import init_paths
from open_shrimp.sandbox.prerequisites import (
    DECLARATIONS,
    DECLARATIONS_BY_BACKEND,
    SandboxBackendDeclaration,
)

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


_Check = Callable[[Config | None], tuple[bool, str]]

_CHECKS: list[tuple[str, _Check]] = [
    ("moonshine-stt", _check_moonshine_stt),
    ("cloudflared", _check_cloudflared),
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
    declaration = DECLARATIONS_BY_BACKEND.get(backend)
    if declaration is None or declaration.platform != current:
        return []
    return list(declaration.checks)


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
    capabilities: frozenset[str] = frozenset()
    base_image_placeholder: str = ""
    unsupported_reasons: tuple[tuple[str, str], ...] = ()


def _offer(
    declaration: SandboxBackendDeclaration,
    config: Config | None,
) -> SandboxOffer | None:
    """What *declaration* would give a context here, or ``None`` if it cannot run.

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
    outcomes = prerequisites(declaration.backend, config)
    return _offer_from_outcomes(declaration, outcomes)


def _offer_from_outcomes(
    declaration: SandboxBackendDeclaration,
    outcomes: list[Outcome],
) -> SandboxOffer | None:
    """Build an offer from prerequisite outcomes already taken for this host."""
    if not outcomes:
        return None
    failed = [o for o in outcomes if not o.ok]
    # The check's own name is dropped where it repeats the backend's, so a
    # single-prerequisite backend reads as "Lima — limactl not found" rather
    # than naming Lima twice in one line.
    return SandboxOffer(
        backend=declaration.backend,
        label=declaration.label,
        summary=declaration.summary,
        capabilities=declaration.capabilities,
        base_image_placeholder=declaration.base_image_placeholder,
        unsupported_reasons=declaration.unsupported_reasons,
        available=not failed,
        detail="; ".join(
            o.detail if o.label == declaration.label else f"{o.label}: {o.detail}"
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
    offers = (_offer(declaration, config) for declaration in DECLARATIONS)
    return [offer for offer in offers if offer is not None]


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
    current = platform.system()
    return next(
        (declaration.backend for declaration in DECLARATIONS
         if declaration.platform == current),
        None,
    )


def blessed_offer(config: Config | None = None) -> SandboxOffer | None:
    """What enabling the sandbox would mean here, and whether it can be honoured.

    ``None`` where this platform has no blessed backend at all; an offer with
    ``available`` false where it has one whose prerequisites are missing.
    ``sandbox_note`` is what a front end should say about either.
    """
    backend = blessed_backend()
    if backend is None:
        return None
    return _offer(DECLARATIONS_BY_BACKEND[backend], config)


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


def sandboxes_payload(
    offers: list[SandboxOffer],
    blessed: SandboxOffer | None,
) -> dict[str, object]:
    """The sandbox catalog contract shared by setup and the config Mini App."""
    return {
        "sandbox": {
            "backend": blessed.backend if blessed is not None else None,
            "available": blessed is not None and blessed.available,
            "note": sandbox_note(blessed),
        },
        "sandboxes": [
            {
                "backend": offer.backend,
                "label": offer.label,
                "summary": offer.summary,
                "capabilities": sorted(offer.capabilities),
                "base_image_placeholder": offer.base_image_placeholder,
                "unsupported_reasons": dict(offer.unsupported_reasons),
                "available": offer.available,
                "detail": offer.detail,
            }
            for offer in offers
        ],
    }


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
    config = _load_config(config_path)
    init_paths(config.instance_name if config is not None else None)
    _use_utf8_output()
    pass_icon, fail_icon = _icons()
    has_failure = False

    sandbox_checks = (
        check
        for declaration in DECLARATIONS
        if platform.system() == declaration.platform
        for check in declaration.checks
    )
    for label, check_fn in (*_CHECKS, *sandbox_checks):
        outcome = run_check(label, check_fn, config)
        icon = pass_icon if outcome.ok else fail_icon
        print(_printable(f"  {icon} {label}: {outcome.detail}"))
        if not outcome.ok:
            has_failure = True

    return 1 if has_failure else 0
