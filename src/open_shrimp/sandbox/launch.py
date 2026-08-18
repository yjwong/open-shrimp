"""Running the sandbox lifecycle in order, and naming what failed.

Two paths start an agent inside a sandbox — a live conversation and an
``ask_context`` sub-query — and both need the same four steps in the same
order, off the event loop, ending in the same typed failure.  Written once so
that a third path cannot get the order right and the failure wrong: a startup
that raises anything else reaches the handler's last resort and is rendered as
"An error occurred while processing your request", which is the answer this
whole path exists to stop giving.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from open_shrimp.sandbox.base import Sandbox, SandboxStartupError

if TYPE_CHECKING:
    from open_shrimp.sandbox.agent_runtime import AgentHandle, AgentRuntime
    from open_shrimp.sandbox.manager import SandboxManager


async def start_sandboxed_agent(
    sandbox: Sandbox,
    runtime: "AgentRuntime",
    *,
    context_name: str,
    backend: str,
    manager: "SandboxManager | None" = None,
    log_file: Path | None = None,
) -> "AgentHandle":
    """Bring *sandbox* up and launch *runtime* inside it.

    Every failure leaves as a :class:`SandboxStartupError` naming the context
    and the backend, which is all a caller needs to ask that backend's
    prerequisite checks what is wrong with the machine.

    *log_file* keeps the build log registered through provisioning, so long
    steps (the one-time Waydroid image download, say) stream to the terminal
    Mini App; it is unregistered as soon as provisioning ends, whether or not
    provisioning succeeded, because a log nobody is writing to is a Mini App
    that never stops loading.  Passing one requires the *manager* that owns it.
    """

    def _start() -> "AgentHandle":
        try:
            sandbox.ensure_environment(log_file=log_file)
            sandbox.ensure_running(log_file=log_file)
            sandbox.provision_workspace(log_file=log_file)
        finally:
            if log_file is not None:
                assert manager is not None
                manager.unregister_build(context_name)
        return sandbox.start_agent(runtime)

    try:
        return await asyncio.to_thread(_start)
    except Exception as e:
        raise SandboxStartupError(context_name, backend, e) from e
