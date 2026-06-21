"""Claude-specific host-side credential watcher body and host paths.

The Claude Code CLI refreshes its OAuth access tokens **independently of
dispatches** — the host CLI rewrites ``~/.claude/.credentials.json``
(Linux/Windows) or bumps the macOS login Keychain whenever it notices an
expired token, including between OpenShrimp turns.  A sandboxed ``claude``
process holding a stale file silently 401s on its next call, so the watcher's
job is to fan host-side refreshes out to every registered sandbox claude-home
in near real time.

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

RUNTIME_NAME = "claude"


def host_credentials_available() -> bool:
    """Whether host-side credentials exist to sync into sandboxes."""
    if sys.platform == "darwin":
        # The login keychain DB always exists for a logged-in user; we don't
        # gate on the actual ``Claude Code-credentials`` entry — if it's
        # missing, the watcher simply won't propagate anything.
        return (MACOS_KEYCHAIN_DIR / MACOS_KEYCHAIN_DB_NAME).exists()
    return HOST_CREDENTIALS.exists()


def write_target(home_dir: Path, payload: str) -> None:
    """Write *payload* into the sandbox's claude-home as ``.credentials.json``."""
    dest = home_dir / ".credentials.json"
    dest.write_text(payload, encoding="utf-8")


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
    """
    if sys.platform == "darwin":
        _watch_credentials_macos(stop)
    else:
        _watch_credentials_linux(stop)


__all__ = [
    "HOST_CREDENTIALS",
    "MACOS_KEYCHAIN_DB_NAME",
    "MACOS_KEYCHAIN_DIR",
    "RUNTIME_NAME",
    "host_credentials_available",
    "watch_host_credentials",
    "write_target",
]
