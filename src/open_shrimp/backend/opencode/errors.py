"""OpenCode-only error types.

The shared ``CLIConnectionError`` / ``ProcessError`` live in
``backend/errors.py`` (the backend-neutral contract every backend raises
through).  Only the two OpenCode-specific subclasses survive here; they
subclass the *shared* ``CLIConnectionError`` so existing
``except backend.errors.CLIConnectionError`` sites catch them unchanged.
"""

from __future__ import annotations

from open_shrimp.backend.errors import CLIConnectionError


class OpenCodeAuthError(CLIConnectionError):
    pass


class OpenCodeNotFoundError(CLIConnectionError):
    pass


__all__ = ["OpenCodeAuthError", "OpenCodeNotFoundError"]
