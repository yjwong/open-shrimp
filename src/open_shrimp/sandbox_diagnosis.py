"""What to say to the operator when a sandbox refuses to start.

A sandbox that will not come up is almost always a machine that is missing
something — a missing virtiofsd, a group membership, the WSL kernel, a
Python binding — and the prerequisite checks behind ``openshrimp doctor`` already
know what and already know the way out.  So the first thing asked on a failure
is not "what did that line fail to do" but "what is wrong with this machine",
which is the question the operator has and the only one with an answer they
can act on.  The exception's own message is rendered either way, after the
remedy where there is one: a check can fail over something this failure had
nothing to do with, and the reply has to survive that.

The remedy sentences here are ``doctor``'s, word for word, and the boot-time
readiness card renders the same ones: the fix must not drift between the three
places it is spoken.  What differs is the frame around it, because the
audiences differ — the card is a checklist for someone who has not tried
anything yet, and this is a reply to someone whose message just failed and who
needs to know that it did not run and what to do about it now.

A check that crashed is not a check that passed.  Where one could not answer,
this refuses to claim the host is fine, and says only what the checks did
establish.
"""

from __future__ import annotations

import asyncio
import time

from open_shrimp.config import Config
from open_shrimp.doctor import Outcome, prerequisites
from open_shrimp.sandbox.base import SandboxStartupError

# How long a diagnosis stands before the checks are run again.  The checks
# shell out and open connections, and a scope that retries in a loop must not
# pay for that on every turn — while a person who has actually fixed something
# takes far longer than this to come back (a group-membership remedy alone
# requires logging out and in again).
_TTL = 30.0

_cache: dict[str, tuple[float, list[Outcome]]] = {}

# One diagnosis per backend at a time: a host fact that breaks takes every
# scope with it, and they arrive together, so without this each one would run
# its own probe and hold a thread for as long as that takes.
_locks: dict[str, asyncio.Lock] = {}


def _now() -> float:
    return time.monotonic()


def _fresh(backend: str) -> list[Outcome] | None:
    """The standing diagnosis for *backend*, while it still stands."""
    cached = _cache.get(backend)
    if cached is None:
        return None
    taken, outcomes = cached
    return outcomes if _now() - taken < _TTL else None


async def _diagnose_host(backend: str, config: Config | None) -> list[Outcome]:
    """What *backend*'s prerequisites say about this machine.

    Answered from the standing diagnosis where there is one — that lookup
    happens here rather than in the worker, so a repeat costs nothing at all.
    """
    standing = _fresh(backend)
    if standing is not None:
        return standing

    async with _locks.setdefault(backend, asyncio.Lock()):
        standing = _fresh(backend)
        if standing is not None:
            return standing
        outcomes = await asyncio.to_thread(prerequisites, backend, config)
        _cache[backend] = (_now(), outcomes)
        return outcomes


async def diagnose(
    error: SandboxStartupError, config: Config | None
) -> tuple[str, bool]:
    """``(what is wrong, a prerequisite is missing)`` for a startup failure.

    The bool is the difference between "this machine is missing something" and
    "nothing on the prerequisite list came back missing", which is what a
    caller needs to say what to do next.
    """
    outcomes = await _diagnose_host(error.backend, config)
    reported = str(error).strip() or "the sandbox did not start"
    failing = [o for o in outcomes if o.ok is False]
    if failing:
        # The remedy leads because it is the actionable half, and the exception
        # follows it rather than being dropped for it: a reply carrying only
        # the check sends the operator to repair something that was not broken,
        # and leaves the one sentence naming what did break in the log.
        return (
            "\n".join(f"{o.label}: {o.detail}" for o in failing)
            + f"\n\nWhat actually failed was: {reported}"
        ), True

    unanswered = [o.label for o in outcomes if o.ok is None]
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

    Plain text, never MarkdownV2 and with nothing in it that only renders as
    markup: the remedies carry Windows paths, shell variables and parentheses,
    and a message Telegram rejects for a bad entity is worse than the generic
    sentence it replaces — the same reason ``mini_app._unavailable_text`` is
    plain.
    """
    what, prerequisite = await diagnose(error, config)
    lead = (
        "Once that's sorted, send your message again."
        if prerequisite
        else (
            "Nothing on the prerequisite list came back missing, so sending "
            "the message again may well work."
        )
    )
    return (
        f'I couldn\'t start the sandbox for the "{error.context_name}" '
        "project, so your message hasn't run.\n\n"
        f"{what}\n\n"
        f"{lead} Meanwhile /context switches to a project that doesn't use a "
        "sandbox, and openshrimp doctor prints the full report."
    )
