"""Keep the host's Claude OAuth token fresh on behalf of sandboxed CLIs.

Claude's refresh tokens rotate: each refresh returns a new one and the old one
answers ``invalid_grant`` from then on, which makes the CLI blank its
credentials file and demand ``/login``.  The CLI serialises refreshes with a
lock inside its own config dir, and a sandbox's ``~/.claude`` is a different
dir from the host's, so a guest CLI holding the same refresh token as the host
can race it.  Guests are therefore given credentials without a refresh token
(:func:`open_shrimp.backend.claude_sdk.cred_watcher.guest_payload`) and the
host is the only side that refreshes.

A host whose contexts are all sandboxed never runs a host CLI that would
notice the token expiring, so this module drives one.  It speaks no OAuth
itself: ``claude mcp get <name>`` refreshes through the CLI's own lock and
storage when the access token is inside the CLI's 5-minute expiry window, and
leaves it alone otherwise.  For a server name that does not exist the command
connects to nothing and exits 1 in under a second; its exit code is
meaningless here, so success is judged by the stored expiry moving forward.
The rewritten store is picked up by the credentials watcher, which fans it out
to every registered sandbox home.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Seconds before expiry at which the host CLI is asked to refresh.  Must sit
# inside the CLI's own 300 s window or the command returns without refreshing.
REFRESH_LEAD_S = 240

# Pause between attempts when a refresh did not move the expiry forward.
RETRY_S = 30

# Longest single sleep.  Re-reading the store at least this often bounds how
# stale a sleep computed before a host-side refresh or a clock jump can get.
MAX_SLEEP_S = 1800

# A name no one would give an MCP server, so the command never connects to one.
PROBE_SERVER_NAME = "openshrimp-token-refresh-probe"

CLI_TIMEOUT_S = 60


def run_refresh_cli(binary: str) -> None:
    """Run the host CLI so it refreshes the token if it is near expiry.

    Runs from an empty temporary directory so no project ``.mcp.json`` is
    picked up.
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryDirectory(prefix="openshrimp-refresh-") as cwd:
        result = subprocess.run(
            [binary, "mcp", "get", PROBE_SERVER_NAME],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            **kwargs,
        )
    logger.debug(
        "Refresh CLI exited %d: %s", result.returncode, result.stderr.strip(),
    )


def keep_host_token_fresh(
    stop: threading.Event,
    read_expires_at: Callable[[], int | None],
    refresh: Callable[[], None],
    *,
    now: Callable[[], float] = time.time,
) -> None:
    """Refresh the host token shortly before each expiry until *stop* is set.

    *read_expires_at* returns the stored access-token expiry in epoch
    milliseconds, or ``None`` when there is no refreshable token (signed out,
    or the CLI blanked a dead token).  *refresh* asks the host CLI to refresh.
    """
    warned_for: int | None = None
    while not stop.is_set():
        try:
            expires_at = read_expires_at()
        except Exception:
            logger.debug("Could not read host token expiry", exc_info=True)
            expires_at = None
        if expires_at is None:
            stop.wait(60)
            continue

        delay = expires_at / 1000 - REFRESH_LEAD_S - now()
        if delay > 0:
            stop.wait(min(delay, MAX_SLEEP_S))
            continue

        try:
            refresh()
        except Exception:
            logger.warning("Host token refresh command failed", exc_info=True)
        try:
            refreshed = read_expires_at()
        except Exception:
            refreshed = None
        if refreshed is not None and refreshed > expires_at:
            logger.info(
                "Refreshed host Claude token; next expiry in %.0f min",
                (refreshed / 1000 - now()) / 60,
            )
            warned_for = None
            continue
        if refreshed is None:
            logger.warning(
                "Host Claude credentials were cleared during refresh; "
                "sign in again with /login",
            )
        elif warned_for != expires_at:
            logger.warning(
                "Host Claude token did not refresh; sandboxed sessions "
                "will fail authentication after it expires. Retrying "
                "every %d s",
                RETRY_S,
            )
            warned_for = expires_at
        stop.wait(RETRY_S)


__all__ = [
    "PROBE_SERVER_NAME",
    "REFRESH_LEAD_S",
    "keep_host_token_fresh",
    "run_refresh_cli",
]
