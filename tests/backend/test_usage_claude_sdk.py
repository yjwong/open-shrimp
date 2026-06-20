"""Tests for the ``claude_sdk`` ``/usage`` fetcher and translator.

Asserted here:

* Anthropic-shaped JSON projects into a :class:`UsageReport` with the
  expected tier labels, percentages, and ``resets_at`` timestamps.
* Missing tiers and missing ``utilization`` keys are omitted.
* ``extra_usage`` with ``monthly_limit=0`` yields no extra line.
* ``~/.claude/.credentials.json`` missing → ``None`` (no HTTP call).
* Expired token (within the 5-minute buffer) → ``None`` without HTTP.
* HTTP 500 → ``None``.
* Module-level 60 s cache: two ``fetch()`` calls within the TTL trigger
  a single HTTP request.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_shrimp.backend.claude_sdk import usage as usage_mod
from open_shrimp.backend.claude_sdk.usage import _to_report, fetch
from open_shrimp.backend.usage import ExtraUsage, UsageReport, UsageTier


# ---------------------------------------------------------------------------
# Translator (_to_report)
# ---------------------------------------------------------------------------


class TestToReport:
    def test_full_anthropic_shape_maps_three_tiers_and_extra(self) -> None:
        data: dict[str, Any] = {
            "five_hour": {
                "utilization": 42.0,
                "resets_at": "2026-06-20T18:00:00+00:00",
            },
            "seven_day": {
                "utilization": 17.0,
                "resets_at": "2026-06-27T18:00:00+00:00",
            },
            "seven_day_sonnet": {
                "utilization": 9.0,
                "resets_at": "2026-06-27T18:00:00+00:00",
            },
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 1234,  # cents
                "monthly_limit": 10000,  # cents
            },
        }
        rep = _to_report(data)
        assert isinstance(rep, UsageReport)
        assert [t.name for t in rep.tiers] == [
            "5-hour session",
            "7-day overall",
            "7-day Sonnet",
        ]
        assert rep.tiers[0].used_pct == pytest.approx(42.0)
        assert rep.tiers[0].resets_at == datetime(
            2026, 6, 20, 18, 0, 0, tzinfo=timezone.utc
        )
        assert isinstance(rep.extra, ExtraUsage)
        assert rep.extra.used_usd == pytest.approx(12.34)
        assert rep.extra.limit_usd == pytest.approx(100.0)

    def test_missing_tier_keys_are_omitted(self) -> None:
        data = {"five_hour": {"utilization": 5.0}}
        rep = _to_report(data)
        assert [t.name for t in rep.tiers] == ["5-hour session"]
        assert rep.extra is None

    def test_utilization_none_skips_tier(self) -> None:
        data = {
            "five_hour": {"utilization": None},
            "seven_day": {"utilization": 33.3},
        }
        rep = _to_report(data)
        assert [t.name for t in rep.tiers] == ["7-day overall"]

    def test_extra_disabled_yields_no_extra(self) -> None:
        data = {
            "extra_usage": {
                "is_enabled": False,
                "used_credits": 100,
                "monthly_limit": 1000,
            },
        }
        assert _to_report(data).extra is None

    def test_extra_with_zero_limit_yields_no_extra(self) -> None:
        data = {
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 0,
                "monthly_limit": 0,
            },
        }
        assert _to_report(data).extra is None

    def test_unparseable_resets_at_is_none(self) -> None:
        data = {"five_hour": {"utilization": 1.0, "resets_at": "not-a-date"}}
        rep = _to_report(data)
        assert rep.tiers[0].resets_at is None


# ---------------------------------------------------------------------------
# Fetch (creds + HTTP + cache)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_usage_cache():
    """Reset the module-level cache before/after each test."""
    usage_mod._reset_cache_for_tests()
    yield
    usage_mod._reset_cache_for_tests()


@pytest.fixture
def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at ``tmp_path`` so ``.credentials.json``
    lookups are deterministic and isolated."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_creds(
    home: Path,
    *,
    token: str = "live-token",
    expires_at_ms: int | None = None,
) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, Any] = {"accessToken": token}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": oauth}), encoding="utf-8"
    )


class _StubTransport(httpx.AsyncBaseTransport):
    """Stub httpx transport that counts calls and returns a canned response."""

    def __init__(
        self,
        *,
        json_body: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> None:
        self.calls = 0
        self._json_body = json_body or {}
        self._status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            self._status_code,
            json=self._json_body,
            request=request,
        )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: _StubTransport
) -> None:
    """Replace ``httpx.AsyncClient`` so every call uses ``transport``."""
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(
        "open_shrimp.backend.claude_sdk.usage.httpx.AsyncClient", factory
    )


@pytest.mark.asyncio
async def test_missing_credentials_returns_none(_fake_home: Path) -> None:
    assert await fetch() is None


@pytest.mark.asyncio
async def test_expired_token_returns_none_without_http(
    _fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1 second in the past — well inside the 5-minute buffer.
    past_ms = int((time.time() - 1) * 1000)
    _write_creds(_fake_home, expires_at_ms=past_ms)
    transport = _StubTransport(json_body={"five_hour": {"utilization": 1.0}})
    _install_transport(monkeypatch, transport)

    assert await fetch() is None
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_http_error_returns_none(
    _fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_creds(_fake_home)
    transport = _StubTransport(status_code=500)
    _install_transport(monkeypatch, transport)

    assert await fetch() is None
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_successful_fetch_returns_report(
    _fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_creds(_fake_home)
    transport = _StubTransport(
        json_body={"five_hour": {"utilization": 42.0}}
    )
    _install_transport(monkeypatch, transport)

    rep = await fetch()
    assert isinstance(rep, UsageReport)
    assert rep.tiers == [UsageTier(name="5-hour session", used_pct=42.0)]


@pytest.mark.asyncio
async def test_cache_avoids_second_http_call(
    _fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_creds(_fake_home)
    transport = _StubTransport(
        json_body={"five_hour": {"utilization": 7.0}}
    )
    _install_transport(monkeypatch, transport)

    first = await fetch()
    second = await fetch()
    assert first == second
    assert transport.calls == 1
