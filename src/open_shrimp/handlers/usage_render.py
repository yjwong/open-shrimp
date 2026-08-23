"""MarkdownV2 rendering for ``/usage``.

Kept in its own module so the renderer is unit-testable in isolation
from the handler. Consumes the backend-neutral :class:`UsageReport`
shape, so it works for any backend that returns one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from open_shrimp.backend.usage import UsageReport, UsageTier
from open_shrimp.markdown import escape


def render_usage_reports(reports: list[tuple[str, UsageReport]]) -> str:
    """Render one or more ``(backend_name, report)`` tuples as MarkdownV2.

    Single report → flat list (today's output, unchanged for users on
    single-backend installs). Multiple reports → one section header per
    backend, sections separated by a blank line.
    """
    if len(reports) == 1:
        _, report = reports[0]
        return "\n".join(_render_report(report))

    sections: list[str] = []
    for name, report in reports:
        header = f"*{escape(name)}*"
        lines = _render_report(report)
        sections.append("\n".join([header, *lines]))
    return "\n\n".join(sections)


def _render_report(report: UsageReport) -> list[str]:
    lines: list[str] = []
    for tier in report.tiers:
        lines.append(_format_tier(tier))
    if report.extra:
        used = report.extra.used_usd
        limit = report.extra.limit_usd
        pct = min(100, used / limit * 100) if limit > 0 else 0
        label = escape(report.extra.label)
        body = escape(f"${used:.2f} / ${limit:.2f} ({pct:.0f}%)")
        lines.append(f"*{label}:* {body}")
    return lines


def _format_tier(tier: UsageTier) -> str:
    used = min(100, tier.used_pct)
    bar = _usage_bar(used)
    line = (
        f"*{escape(tier.name)}:* {bar} "
        f"{escape(f'{used:.0f}% used')}"
    )
    if tier.resets_at is not None:
        delta = tier.resets_at - datetime.now(timezone.utc)
        total = int(delta.total_seconds())
        if total > 0:
            hours, rem = divmod(total, 3600)
            minutes = rem // 60
            if hours > 0:
                line += escape(f" (resets in {hours}h{minutes}m)")
            else:
                line += escape(f" (resets in {minutes}m)")
    return line


def _usage_bar(used: float) -> str:
    filled = round(used / 10)
    return escape("[" + "█" * filled + "░" * (10 - filled) + "]")


__all__ = ["render_usage_reports"]
