"""Monkey-patch the Python Agent SDK to expose modelUsage on ResultMessage.

The CLI sends ``modelUsage`` in result messages but the Python SDK's
``message_parser.parse_message`` discards it.  This module patches both
``ResultMessage`` (adding a ``model_usage`` field) and the parser so the
field is populated.

Import this module early (before any agent queries) for the patch to
take effect::

    import open_udang.sdk_patch  # noqa: F401  — side-effect import
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from claude_agent_sdk import ResultMessage
from claude_agent_sdk._internal import message_parser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step 1: Add model_usage field to ResultMessage dataclass.
# ---------------------------------------------------------------------------

# Add the field descriptor to the dataclass.  We use dataclasses internals
# to append a field with a default value to an already-frozen class.
_new_field = dataclasses.field(default=None)
_new_field.name = "model_usage"
_new_field._field_type = dataclasses._FIELD  # type: ignore[attr-defined]
ResultMessage.__dataclass_fields__["model_usage"] = _new_field  # type: ignore[attr-defined]

# Patch __init__ to accept and store model_usage.
_original_init = ResultMessage.__init__


def _patched_init(self: ResultMessage, *args: Any, **kwargs: Any) -> None:
    model_usage = kwargs.pop("model_usage", None)
    _original_init(self, *args, **kwargs)
    self.model_usage = model_usage  # type: ignore[attr-defined]


ResultMessage.__init__ = _patched_init  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Step 2: Patch parse_message to extract modelUsage from raw data.
# ---------------------------------------------------------------------------

_original_parse_message = message_parser.parse_message


def _patched_parse_message(data: dict[str, Any]) -> Any:
    """Wrapper that extracts modelUsage and passes it to ResultMessage."""
    result = _original_parse_message(data)
    if isinstance(result, ResultMessage) and isinstance(data, dict):
        model_usage = data.get("modelUsage")
        if model_usage is not None:
            result.model_usage = model_usage  # type: ignore[attr-defined]
    return result


message_parser.parse_message = _patched_parse_message

logger.debug("sdk_patch: ResultMessage.model_usage patch applied")
