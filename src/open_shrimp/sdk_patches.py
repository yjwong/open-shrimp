"""Workarounds for claude-agent-sdk bugs.

SubprocessCLITransport uses an anyio TaskGroup for the stderr reader task.
The task group is entered in connect() and exited in close(), but these
often run in different asyncio task contexts (e.g. when an async generator
is finalized).  anyio cancel scopes have task affinity — exiting from a
different task causes _deliver_cancellation to spin at 100% CPU forever.

This is https://github.com/anthropics/claude-agent-sdk-python/issues/810
(the same class of bug as #378 / #454 / #776, fixed in Query by PR #746
but not in SubprocessCLITransport).

The fix: after super().connect() enters the anyio task group (in the
current task frame, so __aexit__ is legal), immediately tear it down and
replace it with a plain asyncio.create_task().  Override close() to cancel
the plain task instead of the task group.

Call ``apply()`` once at startup before any SDK clients are created.
Remove this file once the SDK ships a fix.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

_applied = False


def apply() -> None:
    """Monkey-patch SubprocessCLITransport to avoid the stderr task group bug.

    Safe to call multiple times; only patches once.
    """
    global _applied
    if _applied:
        return

    import claude_agent_sdk._internal.transport.subprocess_cli as mod

    Original = mod.SubprocessCLITransport

    class PatchedSubprocessCLITransport(Original):  # type: ignore[misc]
        """SubprocessCLITransport with the stderr task group replaced by a
        plain asyncio task to avoid the anyio cancel-scope affinity bug."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._stderr_task: asyncio.Task[None] | None = None

        async def connect(self) -> None:
            await super().connect()

            # super().connect() may have created an anyio task group for
            # stderr.  Tear it down *in this same task frame* (so the
            # __aexit__ is legal) and replace with a plain asyncio task.
            tg = self._stderr_task_group
            if tg is not None:
                tg.cancel_scope.cancel()
                with suppress(Exception, BaseException):
                    await tg.__aexit__(None, None, None)
                self._stderr_task_group = None

                # Re-launch the stderr reader as a plain asyncio task.
                self._stderr_task = asyncio.create_task(
                    self._handle_stderr(),
                    name="claude-sdk-stderr-reader",
                )

        async def close(self) -> None:
            # Cancel our plain stderr task before the parent's close()
            # tries to touch the (now-None) task group.
            if self._stderr_task is not None and not self._stderr_task.done():
                self._stderr_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._stderr_task
                self._stderr_task = None

            # The parent's close() will skip the task-group block because
            # _stderr_task_group is None, then close streams / terminate.
            await super().close()

    # Patch the module so ClaudeSDKClient.connect() imports the fixed class.
    mod.SubprocessCLITransport = PatchedSubprocessCLITransport  # type: ignore[misc]
    _applied = True
    logger.info(
        "Patched SubprocessCLITransport stderr task group "
        "(workaround for claude-agent-sdk#810)"
    )
