"""Monkey-patch the Python Agent SDK to expose extra fields the CLI sends.

The CLI sends fields that the Python SDK's ``message_parser.parse_message``
discards:

- ``modelUsage`` on result messages (cumulative token counts, context window).
- ``usage`` on assistant messages (per-turn token counts from the API).

This module patches both ``ResultMessage`` and ``AssistantMessage`` to
expose these fields, and wraps the parser to populate them.

Import this module early (before any agent queries) for the patch to
take effect::

    import open_udang.sdk_patch  # noqa: F401  — side-effect import
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage
from claude_agent_sdk._internal import message_parser

logger = logging.getLogger(__name__)


def _add_optional_field(cls: type, name: str) -> None:
    """Add an optional field (default None) to a dataclass at runtime."""
    new_field = dataclasses.field(default=None)
    new_field.name = name
    new_field._field_type = dataclasses._FIELD  # type: ignore[attr-defined]
    cls.__dataclass_fields__[name] = new_field  # type: ignore[attr-defined]


def _wrap_init(cls: type, field_name: str) -> None:
    """Wrap __init__ to accept and store an extra kwarg."""
    original_init = cls.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        value = kwargs.pop(field_name, None)
        original_init(self, *args, **kwargs)
        setattr(self, field_name, value)

    cls.__init__ = patched_init  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Step 1: Add model_usage field to ResultMessage.
# ---------------------------------------------------------------------------
_add_optional_field(ResultMessage, "model_usage")
_wrap_init(ResultMessage, "model_usage")

# ---------------------------------------------------------------------------
# Step 2: Add usage field to AssistantMessage (per-turn token counts).
# ---------------------------------------------------------------------------
_add_optional_field(AssistantMessage, "usage")
_wrap_init(AssistantMessage, "usage")

# ---------------------------------------------------------------------------
# Step 3: Patch parse_message to extract the extra fields from raw data.
# ---------------------------------------------------------------------------

_original_parse_message = message_parser.parse_message


def _patched_parse_message(data: dict[str, Any]) -> Any:
    """Wrapper that extracts extra fields the SDK normally discards."""
    result = _original_parse_message(data)
    if isinstance(data, dict):
        if isinstance(result, ResultMessage):
            model_usage = data.get("modelUsage")
            if model_usage is not None:
                result.model_usage = model_usage  # type: ignore[attr-defined]
        elif isinstance(result, AssistantMessage):
            usage = data.get("message", {}).get("usage")
            if usage is not None:
                result.usage = usage  # type: ignore[attr-defined]
    return result


message_parser.parse_message = _patched_parse_message

logger.debug("sdk_patch: ResultMessage.model_usage + AssistantMessage.usage patches applied")
