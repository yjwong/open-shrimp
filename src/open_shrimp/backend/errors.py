"""Backend-neutral error contract.

On ``master`` the only backend that *raises* is the SDK, so these names are
**aliases** of the SDK's classes — ``except backend.errors.ProcessError``
catches the identical object the SDK raises, giving zero behavior change.

Step 3 (which owns raising across backends) promotes these to genuine
shared base classes that the SDK errors subclass.  Until then, importing
from here instead of ``claude_agent_sdk`` is purely a source-of-truth move:
it lets ``handlers/messages.py`` name the contract without forking it.
"""

from __future__ import annotations

from claude_agent_sdk import (
    CLIConnectionError as CLIConnectionError,
    ProcessError as ProcessError,
)

__all__ = ["CLIConnectionError", "ProcessError"]
