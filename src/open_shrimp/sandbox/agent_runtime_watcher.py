"""Runtime-agnostic host-side credential watcher plumbing.

The runtime declares *how* to watch (``AgentRuntime.watch_host_credentials``)
and *whether* there are host-side credentials to watch
(``AgentRuntime.host_credentials_available``); this module owns the lazy
start/stop bookkeeping and the per-sandbox fan-out target table.

The watcher thread starts on the first :func:`register_sandbox` call for a
runtime that declares a non-default ``watch_host_credentials`` body, and stops
once the last sandbox is unregistered.  A runtime whose host-side store is
re-read per request (``re_inject_on_dispatch=True``) leaves the watcher hook at
default and never starts a thread here — the dispatcher's per-dispatch
re-inject covers it.

Targets are keyed by ``context_name`` so the registration table mirrors the
sandbox cache.  The watcher body, when it observes a host-side refresh, hands
off to :func:`propagate_credentials`, which fans the new payload out to every
registered home dir.  The runtime supplies the per-target ``write`` callable so
the watcher does not encode the on-disk layout — that is the runtime's
contract.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-target writer: given a host-side credentials payload string, write the
# runtime-specific shape into the registered sandbox home.  Today's claude
# runtime writes ``<home>/.credentials.json`` verbatim; a future runtime could
# transform the payload (e.g. provider-filter it) before writing.
TargetWriter = Callable[[Path, str], None]


@dataclass(frozen=True)
class _Target:
    """A registered sandbox home and its runtime-supplied writer."""

    home_dir: Path
    write: TargetWriter


# Per-runtime registration state.  Keyed by runtime name so two runtimes can
# coexist in the same install without colliding.
_targets: dict[str, dict[str, _Target]] = {}
_stop_events: dict[str, threading.Event] = {}
_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def register_sandbox(
    runtime_name: str,
    context_name: str,
    home_dir: Path,
    *,
    write: TargetWriter,
    watch: Callable[[threading.Event], None],
    host_credentials_available: Callable[[], bool],
) -> None:
    """Register a sandbox for host-side credential syncing.

    Starts the runtime's watcher thread on the first registration; subsequent
    registrations for the same runtime add to the fan-out table without
    spinning a second thread.  No-op when ``host_credentials_available()``
    returns ``False`` — there is nothing to watch.
    """
    if not host_credentials_available():
        return
    with _lock:
        bucket = _targets.setdefault(runtime_name, {})
        bucket[context_name] = _Target(home_dir=home_dir, write=write)

        thread = _threads.get(runtime_name)
        if thread is None or not thread.is_alive():
            stop = threading.Event()
            _stop_events[runtime_name] = stop
            thread = threading.Thread(
                target=watch,
                args=(stop,),
                daemon=True,
                name=f"{runtime_name}-cred-watcher",
            )
            _threads[runtime_name] = thread
            thread.start()
            logger.debug(
                "Started %s credentials watcher thread", runtime_name,
            )


def unregister_sandbox(runtime_name: str, context_name: str) -> None:
    """Unregister a sandbox.  Stops the watcher when no targets remain."""
    with _lock:
        bucket = _targets.get(runtime_name)
        if bucket is None:
            return
        bucket.pop(context_name, None)
        if bucket:
            return
        _targets.pop(runtime_name, None)
        stop = _stop_events.pop(runtime_name, None)
        thread = _threads.pop(runtime_name, None)
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=2)
        logger.debug(
            "Stopped %s credentials watcher thread", runtime_name,
        )


def propagate_credentials(runtime_name: str, payload: str) -> None:
    """Fan a host-side credentials payload out to every registered home.

    Called by the runtime's watcher body.  Each target writer is invoked under
    a best-effort guard so a single failing sandbox does not block the rest.
    """
    with _lock:
        targets = list(_targets.get(runtime_name, {}).items())
    for ctx_name, target in targets:
        try:
            target.write(target.home_dir, payload)
            logger.debug(
                "Synced %s credentials to %s (context %s)",
                runtime_name, target.home_dir, ctx_name,
            )
        except Exception:
            logger.debug(
                "Failed to sync %s credentials for context %s",
                runtime_name, ctx_name, exc_info=True,
            )


__all__ = [
    "TargetWriter",
    "propagate_credentials",
    "register_sandbox",
    "unregister_sandbox",
]
