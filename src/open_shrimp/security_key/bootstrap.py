"""Runtime bootstrap helpers for security-key forwarding."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HelperStartResult:
    started: bool
    error: str | None = None


async def start_vm_helper(
    sandbox: Any,
    *,
    relay_url: str,
    session_id: str,
    token: str,
    context_name: str,
    logger: logging.Logger,
) -> HelperStartResult:
    """Start the VM helper through the sandbox's narrow helper capability."""
    start_helper = getattr(sandbox, "start_security_key_helper", None)
    if start_helper is None:
        return HelperStartResult(started=False)
    try:
        await asyncio.to_thread(
            start_helper,
            relay_url=relay_url,
            session_id=session_id,
            token=token,
        )
        return HelperStartResult(started=True)
    except (NotImplementedError, RuntimeError, OSError) as exc:
        logger.warning(
            "Failed to auto-start security-key helper for %s: %s",
            context_name,
            exc,
        )
        return HelperStartResult(started=False, error=str(exc))
