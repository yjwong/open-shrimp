"""Append-only audit log for sudo-mode (host_bash) tool invocations.

Every approval, denial, or auto-deny of a host-escape command is logged so
the operator has a durable record of what was attempted outside the
sandbox. The log lives under the OpenShrimp data directory resolved by
:func:`open_shrimp.paths.data_dir`, so it is automatically platform-aware
(``~/.local/share/openshrimp`` on Linux, ``~/Library/Application Support/
openshrimp`` on macOS, etc.) and instance-scoped when ``instance_name`` is
set in the config.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import Literal

from open_shrimp.paths import data_dir

logger = logging.getLogger(__name__)

Outcome = Literal["approved", "denied", "timeout"]

_LOG_FILENAME = "sudo.log"
_MAX_COMMAND_BYTES = 4096


def log_path() -> Path:
    """Resolve the audit log path. Lazy so ``init_paths`` has run first."""
    return data_dir() / _LOG_FILENAME


def _format_line(
    ts: str,
    chat_id: int,
    context_name: str,
    outcome: Outcome,
    command: str,
) -> str:
    safe = command.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    if len(safe) > _MAX_COMMAND_BYTES:
        safe = safe[:_MAX_COMMAND_BYTES] + "…(truncated)"
    return f"{ts}\t{chat_id}\t{context_name}\t{outcome}\t{safe}\n"


def _append_sync(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


async def log_sudo(
    chat_id: int,
    context_name: str,
    command: str,
    outcome: Outcome,
) -> None:
    """Append a sudo-mode audit entry. Never raises — failures are logged."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds",
    )
    line = _format_line(ts, chat_id, context_name, outcome, command)
    path = log_path()
    try:
        await asyncio.to_thread(_append_sync, path, line)
    except OSError:
        logger.exception("Failed to append to sudo audit log %s", path)
