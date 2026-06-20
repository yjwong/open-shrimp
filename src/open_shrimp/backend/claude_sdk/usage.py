"""Anthropic OAuth ``/usage`` fetcher for the ``claude_sdk`` backend.

Reads ``~/.claude/.credentials.json``, calls the
``api.anthropic.com/api/oauth/usage`` endpoint, and projects the
Anthropic-shaped response into the backend-neutral :class:`UsageReport`.

A 60-second module-level cache spares the rate-limited endpoint when the
operator hits ``/usage`` repeatedly in quick succession.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from open_shrimp.backend.usage import ExtraUsage, UsageReport, UsageTier

_CACHE_TTL = 60.0
_cache: tuple[float, UsageReport | None] | None = None


_TIER_LABELS: list[tuple[str, str]] = [
    ("five_hour", "5-hour session"),
    ("seven_day", "7-day overall"),
    ("seven_day_sonnet", "7-day Sonnet"),
]


async def fetch() -> UsageReport | None:
    """Return the cached report, refreshing it if the TTL has elapsed."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    report = await _fetch_uncached()
    _cache = (now, report)
    return report


def _reset_cache_for_tests() -> None:
    """Clear the cache. Test-only seam; do not call from production code."""
    global _cache
    _cache = None


async def _fetch_uncached() -> UsageReport | None:
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = creds["claudeAiOauth"]
        token = oauth["accessToken"]
        expires_at = oauth.get("expiresAt")
        if expires_at is not None:
            buffer_ms = 5 * 60 * 1000
            if (time.time() * 1000 + buffer_ms) >= expires_at:
                return None
    except (KeyError, json.JSONDecodeError, OSError):
        return None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None

    return _to_report(data)


def _to_report(data: dict[str, Any]) -> UsageReport:
    tiers: list[UsageTier] = []
    for key, label in _TIER_LABELS:
        raw = data.get(key)
        if not raw or raw.get("utilization") is None:
            continue
        resets_at = _parse_iso(raw.get("resets_at"))
        tiers.append(
            UsageTier(
                name=label,
                used_pct=float(raw["utilization"]),
                resets_at=resets_at,
            )
        )

    extra_raw = data.get("extra_usage")
    extra: ExtraUsage | None = None
    if extra_raw and extra_raw.get("is_enabled"):
        used = (extra_raw.get("used_credits") or 0) / 100
        limit = (extra_raw.get("monthly_limit") or 0) / 100
        if limit > 0:
            extra = ExtraUsage(used_usd=used, limit_usd=limit)
    return UsageReport(tiers=tiers, extra=extra)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


__all__ = ["fetch"]
