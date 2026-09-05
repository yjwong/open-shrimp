"""A snapshot of one context's sandbox, for anything that displays it.

The point of the module is that callers never touch the :class:`Sandbox`
protocol to find out how a context is doing.  ``running()`` is the expensive
one — libvirt does a ``lookupByName`` plus an ``ssh`` process spawn, Lima runs
two ``limactl`` subprocesses, HCS enumerates compute systems and probes vsock —
so it runs off the event loop and behind a short cache, while
``list_port_forwards()`` is a dict copy under a lock and is called directly.

The cache exists because the probe is now behind a button: a refresh a user
taps twice in a second must not open two SSH connections to answer the same
question.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from open_shrimp.config import ContextConfig, is_sandboxed, sandbox_backend
from open_shrimp.sandbox.base import PortForward

logger = logging.getLogger(__name__)

#: How long a ``running()`` result stands in for the next one.  Short enough
#: that a sandbox someone just started reads as running on the second look,
#: long enough that a mashed refresh button costs one probe.
_STATE_TTL_SECONDS = 5.0

_state_cache: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class SandboxSnapshot:
    """What a context's sandbox is doing, as plain data.

    ``state`` is ``"running"``, ``"stopped"``, ``"not started"`` (no runtime
    has been built for this context yet) or ``"unknown"`` (the backend
    refused to say).
    """

    backend: str
    state: str
    forwards: list[PortForward]


def invalidate(context_name: str) -> None:
    """Drop the cached state for *context_name* after starting or stopping it."""
    _state_cache.pop(context_name, None)


async def describe_sandbox(
    managers: dict[str, Any],
    context_name: str,
    ctx: ContextConfig,
    scope_key: str | None = None,
) -> SandboxSnapshot | None:
    """Snapshot *context_name*'s sandbox, or None if the context has none.

    *scope_key* filters the port forwards to one conversation's; pass None
    for every forward the sandbox holds.
    """
    if not is_sandboxed(ctx):
        return None
    backend = sandbox_backend(ctx)
    manager = managers.get(backend)
    active = manager.get_active_sandbox(context_name) if manager is not None else None
    if active is None:
        return SandboxSnapshot(backend=backend, state="not started", forwards=[])

    return SandboxSnapshot(
        backend=backend,
        state=await _probe_state(active, context_name),
        forwards=_port_forwards(active, context_name, scope_key),
    )


async def _probe_state(active: Any, context_name: str) -> str:
    """``running()`` off the event loop, at most once per TTL per context."""
    now = time.monotonic()
    cached = _state_cache.get(context_name)
    if cached is not None and now - cached[0] < _STATE_TTL_SECONDS:
        return cached[1]
    try:
        state = "running" if await asyncio.to_thread(active.running) else "stopped"
    except Exception:
        logger.debug(
            "Could not read sandbox state for %s", context_name, exc_info=True,
        )
        # Not cached: an unknown is a failure to answer, and the next caller
        # deserves a fresh attempt rather than the failure held for 5s.
        return "unknown"
    _state_cache[context_name] = (now, state)
    return state


def _port_forwards(
    active: Any, context_name: str, scope_key: str | None,
) -> list[PortForward]:
    """The live forwards, on the event loop — the registry is a dict copy."""
    if not active.supports_port_forwarding():
        return []
    try:
        return list(active.list_port_forwards(scope_key))
    except Exception:
        logger.debug(
            "Could not list port forwards for %s", context_name, exc_info=True,
        )
        return []
