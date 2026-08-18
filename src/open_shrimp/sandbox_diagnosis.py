"""What to say to the operator when a sandbox refuses to start.

A sandbox that will not come up is almost always a machine that is missing
something — Docker's daemon, a group membership, the WSL kernel, a Python
binding — and the prerequisite checks behind ``openshrimp doctor`` already
know what and already know the way out.  So the first thing asked on a failure
is not "what did that line fail to do" but "what is wrong with this machine",
which is the question the operator has and the only one with an answer they
can act on.  Only when every prerequisite passes is the failure the exception's
own, and then its message is the best information there is and is rendered
rather than discarded.

The remedy sentences here are ``doctor``'s, word for word, and the boot-time
readiness card renders the same ones: the fix must not drift between the three
places it is spoken.  What differs is the frame around it, because the
audiences differ — the card is a checklist for someone who has not tried
anything yet, and this is a reply to someone whose message just failed and who
needs to know that it did not run and what to do about it now.

A check that crashed is not a check that passed.  Where one could not answer,
this refuses to claim the host is fine, exactly as ``readiness`` refuses to.
"""

from __future__ import annotations

import asyncio
import logging
import time

from open_shrimp.config import Config
from open_shrimp.sandbox.base import SandboxStartupError

logger = logging.getLogger(__name__)

# How long a diagnosis stands before the checks are run again.  ``docker
# info`` shells out and the libvirt check opens a connection, and a scope that
# retries in a loop must not pay for that on every turn — while a person who
# has actually fixed something takes far longer than this to come back (the
# docker-group remedy alone requires logging out and in again).
_TTL = 30.0

_cache: dict[str, tuple[float, list[str], list[str]]] = {}


def _now() -> float:
    return time.monotonic()


def _run_checks(backend: str, config: Config | None) -> tuple[list[str], list[str]]:
    """``(failing, unanswered)`` prerequisites for *backend* on this host.

    Blocking: several checks shell out.  Callers reach it through a thread.
    """
    from open_shrimp.doctor import checks_for_backend

    cached = _cache.get(backend)
    if cached is not None and _now() - cached[0] < _TTL:
        return list(cached[1]), list(cached[2])

    failing: list[str] = []
    unanswered: list[str] = []
    for label, check in checks_for_backend(backend):
        try:
            ok, detail = check(config)
        except Exception:
            logger.warning(
                "The %s prerequisite check failed to run", label, exc_info=True
            )
            unanswered.append(label)
            continue
        if not ok:
            failing.append(f"{label}: {detail}")

    _cache[backend] = (_now(), list(failing), list(unanswered))
    return failing, unanswered


def forget_diagnoses() -> None:
    """Drop every cached diagnosis, so the next failure re-runs the checks."""
    _cache.clear()


async def diagnose(
    error: SandboxStartupError, config: Config | None
) -> tuple[str, bool]:
    """``(what is wrong, a prerequisite is missing)`` for a startup failure.

    The bool is the difference between "this machine is missing something" and
    "this machine looks fine and the attempt failed anyway", which is what a
    caller needs to say what to do next.
    """
    failing, unanswered = await asyncio.to_thread(_run_checks, error.backend, config)
    if failing:
        return "\n".join(failing), True

    reported = str(error).strip() or "the sandbox did not start"
    if unanswered:
        # Not a clean bill of health: say so rather than let the silence of a
        # crashed probe read as a pass.
        return (
            f"{reported}\n\n"
            f"(I couldn't check {', '.join(unanswered)}, so I can't tell you "
            "whether this machine has everything the sandbox needs.)"
        ), False
    return reported, False


async def failure_reply(error: SandboxStartupError, config: Config | None) -> str:
    """The whole reply for a scope whose message never ran.

    Plain text, never MarkdownV2: the remedies carry Windows paths, shell
    variables and parentheses, and a message Telegram rejects for a bad entity
    is worse than the generic sentence it replaces — the same reason
    ``mini_app._unavailable_text`` is plain.
    """
    what, prerequisite = await diagnose(error, config)
    if prerequisite:
        closing = (
            "Once that's sorted, send your message again. Meanwhile /context "
            "switches to a project that doesn't use a sandbox, and "
            "`openshrimp doctor` prints the full report."
        )
    else:
        closing = (
            "Everything the sandbox needs looks to be installed, so sending "
            "the message again may well work. If it keeps failing, /context "
            "switches to a project that doesn't use a sandbox, and "
            "`openshrimp doctor` prints the full report."
        )
    return (
        f'I couldn\'t start the sandbox for the "{error.context_name}" '
        "project, so your message hasn't run.\n\n"
        f"{what}\n\n"
        f"{closing}"
    )
