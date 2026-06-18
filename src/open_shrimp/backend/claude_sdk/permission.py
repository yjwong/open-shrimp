"""Neutral → SDK permission-result translation (claude_sdk adapter).

``hooks.make_can_use_tool`` returns ``backend.types`` permission results so the
shared approval path imports no SDK type.  But the SDK consumes the callback's
return value with a hard ``isinstance`` check against *its own* classes and
raises ``TypeError`` otherwise::

    # claude_agent_sdk/_internal/query.py
    if isinstance(response, PermissionResultAllow):      # SDK's class
        ...
    elif isinstance(response, PermissionResultDeny):     # SDK's class
        ...
    else:
        raise TypeError("Tool permission callback must return PermissionResult ...")

So the neutral callback must be wrapped in an SDK-typed adapter before the SDK
ever sees it.  That translation is an SDK-specific delivery concern and lives
here, in the ``claude_sdk`` backend — not in the shared ``hooks`` path.

The input-side ``ToolPermissionContext`` needs no translation: the SDK
constructs it and ``hooks`` only *reads* ``.signal`` / ``.suggestions`` /
``.tool_use_id`` / ``.agent_id``, all present on the SDK type, so it is passed
through unchanged (duck-typed).
"""

from __future__ import annotations

from claude_agent_sdk.types import (
    PermissionResultAllow as SDKAllow,
    PermissionResultDeny as SDKDeny,
)

from open_shrimp.backend import types as bt
from open_shrimp.backend.protocol import CanUseTool


def to_sdk_permission_callback(neutral_cb: CanUseTool) -> CanUseTool:
    """Adapt a ``backend.types``-returning ``can_use_tool`` to the SDK's
    ``isinstance`` contract (``query.py``'s response check).

    The neutral ``reply`` field (``"once" | "always"``) has no SDK analogue in
    the return path — the SDK conveys "always" via ``updated_permissions``, not
    a ``reply`` flag — so it is intentionally dropped.  ``hooks`` never sets it
    on a return today (every site uses the no-arg / ``message=`` constructors);
    the always-vs-once distinction is carried by OpenShrimp's own session-scoped
    ``ApprovalRule`` layer, not the SDK return.
    """

    async def _sdk_cb(tool_name, tool_input, context):
        # ``context`` is the SDK's ToolPermissionContext; hooks reads it via
        # duck-typing, so pass it through unchanged.
        result = await neutral_cb(tool_name, tool_input, context)
        if isinstance(result, bt.PermissionResultAllow):
            return SDKAllow(
                updated_input=result.updated_input,
                updated_permissions=result.updated_permissions,
            )
        if isinstance(result, bt.PermissionResultDeny):
            return SDKDeny(message=result.message, interrupt=result.interrupt)
        raise TypeError(f"unexpected permission result: {type(result)}")

    return _sdk_cb


__all__ = ["to_sdk_permission_callback"]
