"""Backend-neutral error contract.

``CLIConnectionError`` and ``ProcessError`` are aliases of the SDK's
classes — ``except backend.errors.ProcessError`` catches the identical
object the SDK raises. Importing from here instead of ``claude_agent_sdk``
lets ``handlers/messages.py`` name the contract without coupling to a
specific backend.
"""

from __future__ import annotations

from claude_agent_sdk import (
    CLIConnectionError as CLIConnectionError,
    ProcessError as ProcessError,
)


class AgentTurnError(Exception):
    """The agent aborted the turn and reported why; its process is alive.

    Raised for a provider-side failure such as an exhausted usage limit.
    Kept apart from ``ProcessError`` so the handler shows the message
    verbatim instead of treating it as a crashed process or sandbox and
    retrying the turn.
    """


__all__ = ["AgentTurnError", "CLIConnectionError", "ProcessError"]
