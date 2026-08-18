"""What still stands between a running bot and a useful one.

The failures that matter here are the ones that produce no error: a bot that
starts cleanly, reports nothing wrong, and then ignores its operator forever.
Missing credentials surface as an authentication error in reply to the first
real question; a tunnel that never came up surfaces as a Mini App button that
does nothing when tapped; a project directory that was renamed surfaces as
every turn failing.  Each of those is knowable at boot, so each is asked here
and answered in one line the operator can act on.

Three answers, not two.  A check that could not run is not a check that
passed, and reporting it as one would put a green tick beside the very thing
that is broken — so ``UNKNOWN`` is a state of its own, and a caller tracking
what it has already reported leaves an unknown row's record alone rather than
treating silence as good news.

Every check is bounded, in the only sense a caller can rely on: none of them
can take longer than :data:`_BUDGET` to *answer*, because one that has not
answered by then is reported as unknown.  A blocking probe with no timeout of
its own — ``libvirt.open`` is one — is left running in its thread rather than
waited on, since a worker thread cannot be cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse

from open_shrimp.config import Config, effective_backend
from open_shrimp.web_url import is_public_base, public_base

logger = logging.getLogger(__name__)

# Set on the core by whatever spawned it to say that restarting it is already
# somebody's job.  The desktop apps set it; a terminal does not.
SUPERVISED_ENV = "OPENSHRIMP_SUPERVISED"

# What the core says when nothing will bring it back.  The readiness card and
# the wizard that was told not to register autostart report the same fact, so
# they read it from the same place and cannot drift apart.
NO_AUTOSTART = (
    "I stop when the terminal that started me closes. Run "
    "`openshrimp install` there to keep me running after you log in."
)

# How long any one check may take to answer before it is reported as unknown.
_BUDGET = 30.0

# A detail is one actionable sentence, not a log.  Doctor's remedies are joined
# per backend and can run long, and a card Telegram refuses for length is a
# card nobody reads.
_MAX_DETAIL = 400


class State(Enum):
    OK = "ok"
    PROBLEM = "problem"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Row:
    """One line of the checklist."""

    key: str
    label: str
    state: State
    detail: str = ""


# What a check returns: its verdict and the operator's next step, or ``None``
# when there is nothing on this host to have a verdict about.
Verdict = tuple[State, str] | None


def _sign_in(config: Config) -> Verdict:
    """Whether the agent can authenticate at all.

    Asked only of the Claude backend: the SDK ships a bundled ``claude``, so a
    credential-free install boots perfectly and fails on the first question.
    OpenCode holds its own provider credentials and is not covered.
    """
    if not any(
        effective_backend(ctx, config) == "claude_sdk"
        for ctx in config.contexts.values()
    ):
        return None

    # Either environment variable authenticates the CLI on its own, and a
    # headless install is set up with the second one.
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return State.OK, ""

    from open_shrimp.backend.claude_sdk.cred_watcher import host_signed_in

    if host_signed_in():
        return State.OK, ""
    return (
        State.PROBLEM,
        "I can't answer anything until you sign in. Send /login and follow "
        "the window that opens.",
    )


# The edge of a quick tunnel answers 530 for a second or two after cloudflared
# prints the URL, so a single probe would report a healthy tunnel as broken —
# in the one message that is also the operator's first impression.
_PROBE_ATTEMPTS = 3
_PROBE_GAP = 2.0
_PROBE_TIMEOUT = 5.0

_DEAD_BUTTONS = (
    "so the buttons on /login, /review and /config will open to nothing."
)


async def _global_addresses(host: str) -> list[str] | None:
    """*host*'s addresses, or ``None`` when it could not be resolved.

    Resolution is the only way to catch the case a probe from here cannot:
    a split-horizon name or a tailnet address answers this machine and not
    Telegram, which loads a Mini App from its own servers.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    return [str(info[4][0]) for info in infos]


async def _mini_apps(config: Config) -> Verdict:
    """Whether Telegram can open the web pages the bot hands out.

    Reachability is asked of the network rather than of the tunnel process,
    because a live ``cloudflared`` behind a filtered egress and a dead one
    leave the operator in exactly the same place.
    """
    if not is_public_base(config):
        return (
            State.PROBLEM,
            f"Only this machine can reach my web pages, {_DEAD_BUTTONS} The "
            "tunnel didn't start — restart OpenShrimp to try again, or set "
            "review.public_url to an https address of your own.",
        )

    base = public_base(config)
    host = urlparse(base).hostname
    if host:
        addresses = await _global_addresses(host)
        if addresses and not any(
            _is_global(address) for address in addresses
        ):
            return (
                State.PROBLEM,
                f"{host} only resolves onto your own network, so it answers "
                f"here but not from Telegram's servers, {_DEAD_BUTTONS}",
            )

    import httpx

    outcome = ""
    for attempt in range(_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_PROBE_GAP)
        try:
            async with httpx.AsyncClient(
                timeout=_PROBE_TIMEOUT, follow_redirects=True
            ) as client:
                response = await client.head(base)
        except Exception as e:
            # Not just HTTPError: httpx.InvalidURL is a sibling of it, and a
            # malformed public_url must report a dead button rather than
            # vanish as an unknown.
            outcome = f"didn't answer ({type(e).__name__})"
            continue
        if response.status_code < 500:
            return State.OK, ""
        outcome = f"answered {response.status_code}"
    return (
        State.PROBLEM,
        f"{base} {outcome} on {_PROBE_ATTEMPTS} tries, {_DEAD_BUTTONS} "
        "Restarting OpenShrimp gets a fresh tunnel.",
    )


def _is_global(address: str) -> bool:
    try:
        return ip_address(address).is_global
    except ValueError:
        return False


def _project_folders(config: Config) -> Verdict:
    """Whether every project still points somewhere I can work.

    Writability is checked as well as existence: a directory that is only
    readable fails at the first edit rather than at the first turn, which is
    later and less obvious.
    """
    problems: list[str] = []
    for name, ctx in config.contexts.items():
        path = Path(ctx.directory).expanduser()
        if not path.is_dir():
            problems.append(f"{name}: there's nothing at {path}")
        elif not os.access(path, os.W_OK):
            problems.append(f"{name}: I can't write to {path}")

    if problems:
        return (
            State.PROBLEM,
            "; ".join(problems)
            + ". Every turn there will fail — point it somewhere else in "
            "/config.",
        )
    return State.OK, ""


def _sandbox(config: Config) -> Verdict:
    """Whether a configured sandbox has what it needs to boot.

    Only prerequisites are inspected, never the sandbox itself: building one
    is minutes of work and a checklist may not do it.  The remedies are
    ``doctor``'s, so an operator gets one story whichever way they ask — and
    where doctor would run nothing, because the config names a backend this
    platform does not have, there is no verdict to give rather than a pass.
    """
    from open_shrimp.doctor import prerequisites
    from open_shrimp.sandbox.manager import referenced_backends

    backends = referenced_backends(config)
    if not backends:
        return None

    outcomes = [
        outcome
        for backend in sorted(backends)
        for outcome in prerequisites(backend, config)
    ]
    problems = [f"{o.label}: {o.detail}" for o in outcomes if o.ok is False]
    unanswered = [o.label for o in outcomes if o.ok is None]
    ran = len(outcomes)

    if problems:
        return State.PROBLEM, "; ".join(problems)
    if unanswered:
        return State.UNKNOWN, f"I couldn't check {', '.join(unanswered)}."
    if not ran:
        return None
    return State.OK, ""


def _autostart(config: Config) -> Verdict:
    """Whether closing the window would end the conversation.

    Nothing is asked where something above the core already owns restarting
    it — the desktop apps supervise the process they spawn and are their own
    login item, so the answer there is theirs and not the core's.
    """
    if os.environ.get(SUPERVISED_ENV):
        return None

    from open_shrimp.service import is_service_installed

    if is_service_installed(config.instance_name):
        return State.OK, ""
    return State.PROBLEM, NO_AUTOSTART


# The one place a row's key and label are written.  Onceness is tracked against
# these keys by whoever reports the rows, so a key that existed only inside a
# check body could be reported and then never released.
_CHECKS: tuple[tuple[str, str, Callable[[Config], object]], ...] = (
    ("sign_in", "Signed in to Claude", _sign_in),
    ("mini_apps", "Mini Apps open (sign-in, review, settings)", _mini_apps),
    ("project", "Project folder", _project_folders),
    ("sandbox", "Sandbox ready", _sandbox),
    ("autostart", "Keeps running on its own", _autostart),
)

KEYS = tuple(key for key, _, _ in _CHECKS)


async def _run_check(
    key: str, label: str, check: Callable[[Config], object], config: Config
) -> Row | None:
    """One row, whatever the check does — including not answering.

    Synchronous checks go to a thread because several of them shell out, and
    the event loop is serving Telegram while this runs.
    """
    try:
        if asyncio.iscoroutinefunction(check):
            verdict = await asyncio.wait_for(check(config), _BUDGET)
        else:
            verdict = await asyncio.wait_for(
                asyncio.to_thread(check, config), _BUDGET
            )
    except TimeoutError:
        logger.warning("The %s readiness check did not answer in time", key)
        return Row(
            key,
            label,
            State.UNKNOWN,
            f"this check didn't answer within {_BUDGET:.0f} seconds.",
        )
    except Exception:
        logger.warning("The %s readiness check raised", key, exc_info=True)
        return Row(key, label, State.UNKNOWN, "this check couldn't run.")

    if verdict is None:
        return None
    state, detail = verdict  # type: ignore[misc]
    return Row(key, label, state, detail)


async def check_readiness(config: Config) -> list[Row]:
    """Every applicable row, in the order the operator should read them.

    Run together rather than in sequence: the slow ones are slow for
    unrelated reasons, and the whole set is only as slow as the slowest.
    """
    rows = await asyncio.gather(
        *(_run_check(key, label, check, config) for key, label, check in _CHECKS)
    )
    return [row for row in rows if row is not None]


_MARK = {State.OK: "✅", State.PROBLEM: "❌", State.UNKNOWN: "❔"}


def _clamp(detail: str) -> str:
    if len(detail) <= _MAX_DETAIL:
        return detail
    return detail[: _MAX_DETAIL - 1].rstrip() + "…"


def readiness_text(rows: list[Row]) -> str:
    """The checklist as the operator sees it: a mark, a name, and the fix."""
    lines = ["Where things stand:"]
    for row in rows:
        lines.append(f"  {_MARK[row.state]} {row.label}")
        if row.state is not State.OK and row.detail:
            lines.append(f"      {_clamp(row.detail)}")
    return "\n".join(lines)
