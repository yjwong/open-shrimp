"""Claude-specific host-side credential watcher body and host paths.

The Claude Code CLI refreshes its OAuth access tokens **independently of
dispatches** — the host CLI rewrites ``~/.claude/.credentials.json``
(Linux/Windows) or bumps the macOS login Keychain whenever it notices an
expired token, including between OpenShrimp turns.  A sandboxed ``claude``
process holding a stale file silently 401s on its next call, so the watcher's
job is to fan host-side refreshes out to every registered sandbox claude-home
in near real time.  What it writes carries no refresh token: only the host
refreshes (see :mod:`open_shrimp.backend.claude_sdk.host_refresh`).

The runtime-agnostic registration plumbing lives in
:mod:`open_shrimp.sandbox.agent_runtime_watcher`; this module supplies the
claude-specific bodies (host paths, the inotify/FSEvents watcher loop, the
per-target writer, and the host-credentials-available probe) the runtime
declares on its hooks.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

from open_shrimp.sandbox.agent_runtime_watcher import propagate_credentials

logger = logging.getLogger(__name__)

# Host-side credentials file (Linux/Windows; macOS uses the Keychain —
# see :func:`_watch_credentials_macos`).
HOST_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# macOS login Keychain DB path.  Mtime bumps on every Keychain mutation, so
# FSEvents on the parent directory wakes us on token refresh.
MACOS_KEYCHAIN_DIR = Path.home() / "Library" / "Keychains"
MACOS_KEYCHAIN_DB_NAME = "login.keychain-db"

# The Keychain item the host CLI writes its OAuth tokens to.  The name is the
# CLI's, not ours, so it is spelled once.
KEYCHAIN_SERVICE = "Claude Code-credentials"

RUNTIME_NAME = "claude"


def host_credentials_available() -> bool:
    """Whether host-side credentials exist to sync into sandboxes."""
    if sys.platform == "darwin":
        # The login keychain DB always exists for a logged-in user; we don't
        # gate on the actual ``Claude Code-credentials`` entry — if it's
        # missing, the watcher simply won't propagate anything.
        return (MACOS_KEYCHAIN_DIR / MACOS_KEYCHAIN_DB_NAME).exists()
    return HOST_CREDENTIALS.exists()


def host_signed_in() -> bool:
    """Whether the host holds credentials a person would call signed in.

    Distinct from :func:`host_credentials_available`, which asks only whether
    there is something to sync into a sandbox and so answers yes on macOS for
    every logged-in user.  This answer is shown to that user as "you are
    signed in", so the macOS branch asks the Keychain for the item itself —
    for its attributes and never its secret, because reading the secret from
    a binary that did not create the item raises an authorization dialog, and
    nothing that runs at boot may put a modal on someone's screen.

    The Keychain is not the last word even there: the host CLI falls back to
    writing the credentials file when a Keychain write fails, and a user whose
    agent authenticates perfectly must not be told to sign in again.
    """
    if sys.platform != "darwin":
        return host_credentials_available()

    import getpass
    import subprocess

    try:
        found = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
            ],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("Could not ask the Keychain about credentials", exc_info=True)
        found = False
    return found or HOST_CREDENTIALS.exists()


def guest_payload(payload: str) -> str:
    """Strip the refresh token from a host credentials payload.

    A guest CLI without a refresh token never refreshes (it reports
    ``no_refresh_token`` and keeps using the access token), so it cannot
    rotate the token out from under the host.  It re-reads the file when its
    mtime changes, which is how host refreshes reach it.
    """
    try:
        creds = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    oauth = creds.get("claudeAiOauth")
    if isinstance(oauth, dict):
        oauth.pop("refreshToken", None)
    return json.dumps(creds)


def write_guest_credentials(dest: Path, payload: str) -> None:
    """Write the guest-safe form of *payload* to *dest*, mode 0600."""
    dest.write_text(guest_payload(payload), encoding="utf-8")
    dest.chmod(0o600)


def write_target(home_dir: Path, payload: str) -> None:
    """Write *payload* into the sandbox's claude-home as ``.credentials.json``."""
    write_guest_credentials(home_dir / ".credentials.json", payload)


def read_host_expires_at() -> int | None:
    """The host access token's expiry in epoch ms, if it can be refreshed."""
    if sys.platform == "darwin":
        from open_shrimp.sandbox.lima_helpers import _read_credentials_json

        payload = _read_credentials_json()
    else:
        try:
            payload = HOST_CREDENTIALS.read_text(encoding="utf-8")
        except OSError:
            return None
    if not payload:
        return None
    oauth = json.loads(payload).get("claudeAiOauth") or {}
    if not oauth.get("refreshToken") or not oauth.get("expiresAt"):
        return None
    return int(oauth["expiresAt"])


def _watch_credentials_linux(stop: threading.Event) -> None:
    """Watch ``~/.claude/.credentials.json`` for atomic-replace writes.

    Watches the **parent directory** rather than the credentials file itself
    because Claude Code refreshes credentials via atomic replace (write tmp +
    rename).  Watching the file directly loses track after the first rename —
    inotify is bound to the old inode.
    """
    from watchfiles import watch

    cred_dir = HOST_CREDENTIALS.parent
    cred_name = HOST_CREDENTIALS.name

    if not cred_dir.exists():
        return

    try:
        for changes in watch(
            cred_dir, stop_event=stop, rust_timeout=1000,
        ):
            if stop.is_set():
                break
            if not any(Path(path).name == cred_name for _ct, path in changes):
                continue
            if not HOST_CREDENTIALS.exists():
                continue
            try:
                payload = HOST_CREDENTIALS.read_text(encoding="utf-8")
            except OSError:
                continue
            propagate_credentials(RUNTIME_NAME, payload)
    except Exception:
        if not stop.is_set():
            logger.debug("Credentials watcher exited", exc_info=True)


def _watch_credentials_macos(stop: threading.Event) -> None:
    """Watch the macOS login Keychain for ``Claude Code-credentials`` updates.

    The Claude Code app on macOS stores OAuth tokens in the login Keychain
    rather than ``~/.claude/.credentials.json``.  Any Keychain mutation
    rewrites ``login.keychain-db``, so FSEvents on the Keychains directory
    wakes us on token refresh.  Re-extracts via ``security`` and only
    propagates when the parsed ``expiresAt`` differs from the last known
    value, which filters out noise from unrelated keychain activity (Safari
    saving passwords, etc.).
    """
    from watchfiles import watch

    from open_shrimp.sandbox.lima_helpers import _read_credentials_json

    if not MACOS_KEYCHAIN_DIR.exists():
        return

    last_expires_at: int | None = None

    try:
        for changes in watch(
            MACOS_KEYCHAIN_DIR, stop_event=stop, rust_timeout=1000,
        ):
            if stop.is_set():
                break
            if not any(
                Path(path).name == MACOS_KEYCHAIN_DB_NAME
                for _ct, path in changes
            ):
                continue
            payload = _read_credentials_json()
            if not payload:
                continue
            try:
                expires_at = int(
                    json.loads(payload)
                    .get("claudeAiOauth", {})
                    .get("expiresAt", 0)
                )
            except (ValueError, json.JSONDecodeError):
                continue
            if expires_at == last_expires_at:
                continue
            last_expires_at = expires_at
            propagate_credentials(RUNTIME_NAME, payload)
    except Exception:
        if not stop.is_set():
            logger.debug("Keychain credentials watcher exited", exc_info=True)


def watch_host_credentials(stop: threading.Event) -> None:
    """Background thread: sync host credentials into all active sandboxes.

    Keeps long-lived sandboxed claude clients in sync with host-side token
    refreshes.  Uses native OS change-notification (FSEvents on macOS, inotify
    on Linux) so we wake immediately on refresh rather than polling.

    Guests hold no refresh token, so a sibling thread makes the host CLI
    refresh before each expiry (:mod:`host_refresh`); the watcher then carries
    the result into every sandbox.
    """
    from open_shrimp.backend.claude_sdk.binary import find_claude_binary
    from open_shrimp.backend.claude_sdk.host_refresh import (
        keep_host_token_fresh,
        run_refresh_cli,
    )

    # The refresher lives exactly as long as this watcher body.  The
    # registration plumbing restarts a watcher that exited on its own with a
    # fresh stop event, so sharing *stop* would strand the old refresher.
    refresher_stop = threading.Event()
    try:
        binary = find_claude_binary()
    except RuntimeError:
        logger.warning(
            "No host Claude CLI to refresh tokens with; sandboxed sessions "
            "will need /login once the current token expires",
        )
    else:
        threading.Thread(
            target=keep_host_token_fresh,
            args=(
                refresher_stop,
                read_host_expires_at,
                lambda: run_refresh_cli(binary),
            ),
            daemon=True,
            name="claude-token-refresher",
        ).start()

    try:
        if sys.platform == "darwin":
            _watch_credentials_macos(stop)
        else:
            _watch_credentials_linux(stop)
    finally:
        refresher_stop.set()


__all__ = [
    "HOST_CREDENTIALS",
    "KEYCHAIN_SERVICE",
    "MACOS_KEYCHAIN_DB_NAME",
    "MACOS_KEYCHAIN_DIR",
    "RUNTIME_NAME",
    "guest_payload",
    "host_credentials_available",
    "host_signed_in",
    "read_host_expires_at",
    "watch_host_credentials",
    "write_guest_credentials",
    "write_target",
]
