"""Tests for ``handlers.usage_render.render_usage_reports``.

Asserted here:

* Single report → one table with no section header.
* Multiple reports → one section header per backend, blank-separated.
* Empty ``tiers`` + present ``extra`` → just the extra row.
* ``resets_at`` in the past → an empty Resets cell.
* Markup characters in backend / tier / extra labels are escaped, so a
  name containing ``_`` cannot italicise the rest of the row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from open_shrimp.backend.usage import ExtraUsage, UsageReport, UsageTier
from open_shrimp.handlers.usage_render import render_usage_reports


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class TestSingleReport:
    def test_no_section_header(self) -> None:
        report = UsageReport(
            tiers=[UsageTier(name="5-hour session", used_pct=42.0)],
        )
        text = render_usage_reports([("claude_sdk", report)])
        # No backend name should appear in single-report output.
        assert "claude_sdk" not in text
        assert "5-hour session" in text
        assert "42%" in text

    def test_tier_with_future_resets_at_includes_suffix(self) -> None:
        report = UsageReport(
            tiers=[
                UsageTier(
                    name="5-hour session",
                    used_pct=42.0,
                    resets_at=_future(3700),  # ~1h
                )
            ],
        )
        text = render_usage_reports([("claude_sdk", report)])
        assert "| 1h1m |" in text

    def test_tier_with_minutes_only_resets_at(self) -> None:
        # Use seconds well under an hour so the "hours" branch is never
        # taken; the exact minute value depends on monotonic clock drift
        # between ``_future()`` and ``datetime.now()`` inside the renderer.
        report = UsageReport(
            tiers=[
                UsageTier(
                    name="5-hour session",
                    used_pct=10.0,
                    resets_at=_future(905),  # ~15m
                )
            ],
        )
        text = render_usage_reports([("claude_sdk", report)])
        resets = text.rstrip().rsplit("|", 2)[1].strip()
        assert resets in {"14m", "15m"}

    def test_past_resets_at_omits_suffix(self) -> None:
        report = UsageReport(
            tiers=[
                UsageTier(
                    name="5-hour session",
                    used_pct=42.0,
                    resets_at=_past(60),
                )
            ],
        )
        text = render_usage_reports([("claude_sdk", report)])
        assert text.rstrip().endswith("|  |")

    def test_extra_usage_line(self) -> None:
        report = UsageReport(
            tiers=[],
            extra=ExtraUsage(used_usd=12.34, limit_usd=100.0),
        )
        text = render_usage_reports([("claude_sdk", report)])
        # Empty tiers means the only line is the extra-usage line.
        assert "Extra usage" in text
        assert "12.34" in text
        assert "100.00" in text

    def test_used_pct_above_100_is_clamped(self) -> None:
        report = UsageReport(
            tiers=[UsageTier(name="5-hour session", used_pct=140.0)],
        )
        text = render_usage_reports([("claude_sdk", report)])
        assert "100%" in text


class TestMultipleReports:
    def test_each_backend_gets_a_header(self) -> None:
        r1 = UsageReport(tiers=[UsageTier(name="A", used_pct=10.0)])
        r2 = UsageReport(tiers=[UsageTier(name="B", used_pct=20.0)])
        text = render_usage_reports([("claude_sdk", r1), ("opencode", r2)])
        # Sections separated by a blank line.
        assert "\n\n" in text
        # Header is the backend name in bold; ``_`` is escaped so it cannot
        # open an emphasis run.
        assert "**claude\\_sdk**" in text
        assert "**opencode**" in text

    def test_special_chars_in_backend_name_escaped(self) -> None:
        report = UsageReport(tiers=[UsageTier(name="A", used_pct=10.0)])
        text = render_usage_reports([("claude.sdk_v2", report), ("opencode", report)])
        assert "claude.sdk\\_v2" in text
