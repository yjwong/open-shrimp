"""Anthropic OAuth ``/usage`` fetcher for the ``claude_sdk`` backend.

Reads ``~/.claude/.credentials.json``, calls the
``api.anthropic.com/api/oauth/usage`` endpoint, and projects the
Anthropic-shaped response into the backend-neutral :class:`UsageReport`.

The windows come from the response's ``limits`` array, which names a
per-model weekly window through ``scope.model.display_name`` rather than
through a top-level key: the Fable weekly limit arrives as ``{"kind":
"weekly_scoped", "scope": {"model": {"display_name": "Fable"}}}`` while
the top-level ``seven_day_opus``/``seven_day_sonnet`` keys are ``null``.
Reading only those keys therefore drops every window the account has
beyond the session and all-model ones. The keys are still the fallback
for a response that carries no ``limits``.

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


_LEGACY_TIER_LABELS: list[tuple[str, str]] = [
    ("five_hour", "5-hour session"),
    ("seven_day", "7-day overall"),
    ("seven_day_sonnet", "7-day Sonnet"),
]

_LIMIT_KIND_LABELS: dict[str, str] = {
    "session": "5-hour session",
    "weekly_all": "7-day overall",
}


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
    tiers = _tiers_from_limits(data.get("limits")) or _tiers_from_keys(data)

    extra_raw = data.get("extra_usage")
    extra: ExtraUsage | None = None
    if extra_raw and extra_raw.get("is_enabled"):
        used = (extra_raw.get("used_credits") or 0) / 100
        limit = (extra_raw.get("monthly_limit") or 0) / 100
        if limit > 0:
            extra = ExtraUsage(used_usd=used, limit_usd=limit)
    return UsageReport(tiers=tiers, extra=extra)


def _tiers_from_limits(limits: Any) -> list[UsageTier]:
    """Project the ``limits`` array, preserving the server's order."""
    if not isinstance(limits, list):
        return []
    tiers: list[UsageTier] = []
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("percent") is None:
            continue
        tiers.append(
            UsageTier(
                name=_limit_label(entry),
                used_pct=float(entry["percent"]),
                resets_at=_parse_iso(entry.get("resets_at")),
            )
        )
    return tiers


def _limit_label(entry: dict[str, Any]) -> str:
    """Name one ``limits`` entry from its ``kind`` and scoped model.

    An unrecognised ``kind`` is labelled from its own fields rather than
    dropped, so a window the account gains later still shows up.
    """
    kind = str(entry.get("kind") or "limit")
    scope = entry.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    if kind == "weekly_scoped":
        return f"7-day {model}" if model else "7-day scoped"
    base = _LIMIT_KIND_LABELS.get(kind, kind.replace("_", " "))
    return f"{base} ({model})" if model else base


def _tiers_from_keys(data: dict[str, Any]) -> list[UsageTier]:
    """Project the top-level per-window keys, for a response with no
    ``limits`` array."""
    tiers: list[UsageTier] = []
    for key, label in _LEGACY_TIER_LABELS:
        raw = data.get(key)
        if not raw or raw.get("utilization") is None:
            continue
        tiers.append(
            UsageTier(
                name=label,
                used_pct=float(raw["utilization"]),
                resets_at=_parse_iso(raw.get("resets_at")),
            )
        )
    return tiers


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


__all__ = ["fetch"]
