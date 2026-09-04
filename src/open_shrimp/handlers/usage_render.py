"""Rich-message rendering for ``/usage``.

Kept in its own module so the renderer is unit-testable in isolation
from the handler. Consumes the backend-neutral :class:`UsageReport`
shape, so it works for any backend that returns one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from open_shrimp.backend.usage import UsageReport, UsageTier
from open_shrimp.markdown import escape_rich_inline


def render_usage_reports(reports: list[tuple[str, UsageReport]]) -> str:
    """Render one or more ``(backend_name, report)`` tuples as a table.

    Single report → one table. Multiple reports → one section header per
    backend, sections separated by a blank line.
    """
    if len(reports) == 1:
        _, report = reports[0]
        return _render_report(report)

    sections: list[str] = []
    for name, report in reports:
        header = f"**{escape_rich_inline(name)}**"
        sections.append(f"{header}\n\n{_render_report(report)}")
    return "\n\n".join(sections)


def _render_report(report: UsageReport) -> str:
    rows: list[tuple[str, str, str]] = []
    for tier in report.tiers:
        rows.append(_tier_row(tier))
    if report.extra:
        used = report.extra.used_usd
        limit = report.extra.limit_usd
        pct = min(100, used / limit * 100) if limit > 0 else 0
        rows.append((
            escape_rich_inline(report.extra.label),
            f"{_usage_bar(pct)} ${used:.2f} / ${limit:.2f}",
            "",
        ))

    if not rows:
        return "No usage reported."

    lines = ["| Limit | Used | Resets |", "| :--- | :--- | ---: |"]
    lines.extend(f"| {name} | {used} | {resets} |" for name, used, resets in rows)
    return "\n".join(lines)


def _tier_row(tier: UsageTier) -> tuple[str, str, str]:
    used = min(100, tier.used_pct)
    resets = ""
    if tier.resets_at is not None:
        delta = tier.resets_at - datetime.now(timezone.utc)
        total = int(delta.total_seconds())
        if total > 0:
            hours, rem = divmod(total, 3600)
            minutes = rem // 60
            resets = f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"
    return (
        escape_rich_inline(tier.name),
        f"{_usage_bar(used)} {used:.0f}%",
        resets,
    )


def _usage_bar(used: float) -> str:
    filled = round(used / 10)
    return "█" * filled + "░" * (10 - filled)


__all__ = ["render_usage_reports"]
