"""Backend-neutral error contract.

These names are aliases of the SDK's classes — ``except
backend.errors.ProcessError`` catches the identical object the SDK raises.
Importing from here instead of ``claude_agent_sdk`` lets
``handlers/messages.py`` name the contract without coupling to a specific
backend.
"""

from __future__ import annotations

from claude_agent_sdk import (
    CLIConnectionError as CLIConnectionError,
    ProcessError as ProcessError,
)

__all__ = ["CLIConnectionError", "ProcessError"]
