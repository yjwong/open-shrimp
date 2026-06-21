"""The OpenCode backend adapter.

Contains: the ``OpenCodeClient`` (a ``BackendClient``), options translation
(``to_opencode``), SSE-event → ``backend.types`` translation, the
``PermissionBridge``, the host-local ``OpenCodeServer``, and the
``OpenCodeBackend`` impl wired in ``backend.py`` and registered in
``backend/factory.py``.
"""

from __future__ import annotations

from open_shrimp.backend.opencode.backend import OpenCodeBackend
from open_shrimp.backend.opencode.client import OpenCodeClient

__all__ = ["OpenCodeBackend", "OpenCodeClient"]
