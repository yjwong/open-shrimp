"""The control methods a supervising UI may call.

Deliberately small: process control and nothing else.  Anything that needs to
work while the core is *stopped* — first-run config, diagnostics — belongs on
the CLI instead, because there is no channel to answer on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from open_shrimp.control import protocol
from open_shrimp.control.server import MethodHandler


@dataclass
class CoreStatus:
    """Mutable view of the core that the control channel reports.

    The owner mutates fields as the bot progresses; handlers read them.
    """

    state: str = "starting"
    config_path: str = ""
    instance_name: str | None = None
    contexts: list[str] = field(default_factory=list)
    bot_username: str | None = None
    error: str | None = None


def build_methods(status: CoreStatus) -> dict[str, MethodHandler]:
    async def _status(_params: dict[str, Any]) -> dict[str, Any]:
        from open_shrimp.updater import get_current_version

        return {
            "protocol": protocol.PROTOCOL_VERSION,
            "version": get_current_version(),
            "pid": os.getpid(),
            "state": status.state,
            "config_path": status.config_path,
            "instance_name": status.instance_name,
            "contexts": list(status.contexts),
            "bot_username": status.bot_username,
            "error": status.error,
        }

    async def _shutdown(_params: dict[str, Any]) -> dict[str, Any]:
        # The reply is written before the core unwinds, so the caller learns
        # the request was accepted rather than seeing the channel drop.
        from open_shrimp.main import request_shutdown

        status.state = "stopping"
        request_shutdown()
        return {"accepted": True}

    async def _restart(_params: dict[str, Any]) -> dict[str, Any]:
        from open_shrimp.main import request_restart, request_shutdown

        status.state = "stopping"
        request_restart()
        request_shutdown()
        return {"accepted": True}

    return {"status": _status, "shutdown": _shutdown, "restart": _restart}
