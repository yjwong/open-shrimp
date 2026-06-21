"""End-to-end tests for ``/usage`` over the backend union.

Asserted here:

* No backend declares ``"usage"`` → "not available on …" reply naming
  each configured backend.
* One backend declares capability and returns data → rendered text.
* Two backends both capable, both return data → both sections rendered.
* One capable backend returns ``None`` → "Usage data unavailable" reply.
* Capable backend mixed with a non-capable one → only the capable
  backend's section is included (no header).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from open_shrimp.backend.usage import UsageReport, UsageTier
from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.handlers.commands import usage_handler


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="test",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(),
    )


@dataclass
class _CapturedReply:
    text: str
    parse_mode: str | None = None


class _StubMessage:
    def __init__(self) -> None:
        self.chat_id = 100
        self.message_thread_id = None
        self.replies: list[_CapturedReply] = []

    async def reply_text(
        self, text: str, parse_mode: str | None = None, **_: Any
    ) -> None:
        self.replies.append(_CapturedReply(text=text, parse_mode=parse_mode))


class _StubUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


class _StubUpdate:
    def __init__(self, user_id: int = 1) -> None:
        self.effective_message = _StubMessage()
        self.effective_user = _StubUser(user_id)


class _StubContext:
    def __init__(self, *, config: Config, backends: list[Any]) -> None:
        self.bot_data = {"config": config, "backends": backends}


class _StubBackend:
    """Implements just the protocol surface ``usage_handler`` touches."""

    def __init__(
        self,
        name: str,
        *,
        capable: bool,
        report: UsageReport | None,
    ) -> None:
        self.name = name
        self._capable = capable
        self._report = report

    def command_capabilities(self) -> set[str]:
        return {"usage"} if self._capable else set()

    async def usage(self) -> UsageReport | None:
        return self._report


@pytest.mark.asyncio
async def test_no_capable_backend_replies_not_available() -> None:
    backends = [_StubBackend("opencode", capable=False, report=None)]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    assert "not available" in reply.text
    assert "`opencode`" in reply.text
    assert reply.parse_mode == "MarkdownV2"


@pytest.mark.asyncio
async def test_no_backends_at_all_replies_this_install() -> None:
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=[])

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    assert "this install" in reply.text


@pytest.mark.asyncio
async def test_single_capable_backend_renders_flat_text() -> None:
    report = UsageReport(
        tiers=[UsageTier(name="5-hour session", used_pct=42.0)]
    )
    backends = [_StubBackend("claude_sdk", capable=True, report=report)]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    # No section header for a single report.
    assert "claude_sdk" not in reply.text
    assert "5\\-hour session" in reply.text
    assert "42% used" in reply.text


@pytest.mark.asyncio
async def test_two_capable_backends_each_get_a_section() -> None:
    r1 = UsageReport(tiers=[UsageTier(name="A", used_pct=10.0)])
    r2 = UsageReport(tiers=[UsageTier(name="B", used_pct=20.0)])
    backends = [
        _StubBackend("claude_sdk", capable=True, report=r1),
        _StubBackend("opencode", capable=True, report=r2),
    ]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    # ``_`` is MarkdownV2-escaped in the backend-name header.
    assert "*claude\\_sdk*" in reply.text
    assert "*opencode*" in reply.text


@pytest.mark.asyncio
async def test_capable_plus_incapable_renders_single_section() -> None:
    report = UsageReport(tiers=[UsageTier(name="A", used_pct=10.0)])
    backends = [
        _StubBackend("claude_sdk", capable=True, report=report),
        _StubBackend("opencode", capable=False, report=None),
    ]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    # Single capable backend → no headers; opencode is filtered out.
    assert "*claude\\_sdk*" not in reply.text
    assert "*opencode*" not in reply.text


@pytest.mark.asyncio
async def test_capable_backend_returning_none_replies_unavailable() -> None:
    backends = [_StubBackend("claude_sdk", capable=True, report=None)]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    assert "Usage data unavailable" in reply.text


@pytest.mark.asyncio
async def test_capable_backend_returning_empty_report_replies_unavailable() -> None:
    """Empty tiers + no extra is treated as "no data" — same path as
    ``None`` from the handler's perspective."""
    backends = [
        _StubBackend(
            "claude_sdk", capable=True, report=UsageReport(tiers=[])
        )
    ]
    update = _StubUpdate()
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    [reply] = update.effective_message.replies
    assert "Usage data unavailable" in reply.text


@pytest.mark.asyncio
async def test_unauthorized_user_gets_no_reply() -> None:
    backends = [_StubBackend("claude_sdk", capable=True, report=None)]
    update = _StubUpdate(user_id=999)  # not in allowed_users
    ctx = _StubContext(config=_config(), backends=backends)

    await usage_handler(update, ctx)  # type: ignore[arg-type]

    assert update.effective_message.replies == []
