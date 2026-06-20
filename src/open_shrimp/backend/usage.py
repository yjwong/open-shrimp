"""Backend-neutral usage-report data shapes for ``/usage``.

Kept out of :mod:`backend.types` to avoid bloating the hot-path message
shapes — ``/usage`` is invoked manually and its data flows through one
handler, not the streaming layer.

The dataclasses are deliberately Claude-shaped because the Anthropic
report is the only concrete instance today; backends with a different
notion project a best-effort tier (e.g. monthly spend as a single
:class:`UsageTier`). When a backend's notion is fundamentally
incompatible it returns ``None`` from :meth:`Backend.usage` and falls
through the "not available" path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class UsageTier:
    """One quota window (e.g. "5-hour session", "7-day overall").

    ``used_pct`` is 0–100 inclusive; renderer clamps to 100 when
    drawing the bar. ``resets_at`` is the absolute UTC time the window
    resets, or ``None`` if the backend doesn't expose one.
    """

    name: str
    used_pct: float
    resets_at: datetime | None = None


@dataclass(frozen=True)
class ExtraUsage:
    """Overuse / pay-as-you-go billing line. Optional per report."""

    used_usd: float
    limit_usd: float
    label: str = "Extra usage"


@dataclass(frozen=True)
class UsageReport:
    """A backend's snapshot of the operator's quota / spend.

    ``tiers`` may be empty (e.g. backend reports only ``extra``). The
    handler treats an entirely empty report as "no data" and omits the
    backend's section from the output.
    """

    tiers: list[UsageTier]
    extra: ExtraUsage | None = None


__all__ = ["ExtraUsage", "UsageReport", "UsageTier"]
