"""Cron expression handling for scheduled tasks.

APScheduler 3.x numbers weekdays 0=Monday while crontab numbers them
0=Sunday, so schedule expressions are translated before they reach a
trigger.  These tests assert the days a trigger actually fires on, rather
than the translated spelling, so they stay honest if the translation
strategy changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from open_shrimp.events.schedule import (
    build_cron_trigger,
    parse_once_datetime,
    validate_schedule,
)


def _fire_days(expr: str, tz: str = "UTC") -> set[str]:
    """The weekday abbreviations a cron expression actually fires on."""
    trigger = build_cron_trigger(expr, tz)
    days: set[str] = set()
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(30):
        moment = trigger.get_next_fire_time(moment, moment)
        if moment is None:
            break
        days.add(moment.astimezone(ZoneInfo(tz)).strftime("%a"))
    return days


WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri"}
ALL_DAYS = WEEKDAYS | {"Sat", "Sun"}


@pytest.mark.parametrize(
    ("day_of_week", "expected"),
    [
        # The plain forms, where the two conventions disagree outright.
        ("1-5", WEEKDAYS),
        ("2", {"Tue"}),
        ("0", {"Sun"}),
        ("7", {"Sun"}),  # crontab spells Sunday both ways
        ("6,0", {"Sat", "Sun"}),
        ("*", ALL_DAYS),
        ("1-5,0", WEEKDAYS | {"Sun"}),
        # Ranges that touch Sunday have no direct APScheduler spelling.
        ("0-4", {"Sun", "Mon", "Tue", "Wed", "Thu"}),
        ("6-6", {"Sat"}),
        ("5-1", {"Fri", "Sat", "Sun", "Mon"}),  # wraps around the weekend
        # Steps are anchored on the start of the crontab week, not Monday.
        ("*/2", {"Sun", "Tue", "Thu", "Sat"}),
        ("*/1", ALL_DAYS),
        ("1-5/2", {"Mon", "Wed", "Fri"}),
        ("0-6/3", {"Sun", "Wed", "Sat"}),
        ("5/2", {"Fri", "Sun"}),  # bare N/S runs to the end of the week
        ("5/1", {"Fri", "Sat", "Sun"}),
        ("0/2", {"Sun", "Tue", "Thu", "Sat"}),
        # Names mean the same thing under both conventions.
        ("mon-fri", WEEKDAYS),
        ("sat,0", {"Sat", "Sun"}),
    ],
)
def test_day_of_week_follows_crontab_convention(day_of_week, expected):
    assert _fire_days(f"0 9 * * {day_of_week}") == expected


def test_weekday_schedule_skips_the_weekend():
    """The regression: '1-5' must not fire Saturday, and must fire Monday."""
    days = _fire_days("0 9 * * 1-5", "Asia/Singapore")
    assert "Sat" not in days
    assert "Sun" not in days
    assert "Mon" in days


def test_cron_fires_at_wall_clock_time_in_configured_zone():
    trigger = build_cron_trigger("0 9 * * 1-5", "Asia/Singapore")
    moment = trigger.get_next_fire_time(None, datetime(2026, 8, 1, tzinfo=timezone.utc))
    local = moment.astimezone(ZoneInfo("Asia/Singapore"))
    assert (local.hour, local.minute) == (9, 0)
    assert moment.astimezone(timezone.utc).hour == 1  # 09:00 +08


@pytest.mark.parametrize(
    "expr", ["0 9 * * 8", "0 9 * *", "0 9 * * 1-5 extra", "0 9 * * "]
)
def test_malformed_cron_is_rejected(expr):
    with pytest.raises(ValueError):
        build_cron_trigger(expr)


def test_validate_rejects_too_frequent_cron():
    with pytest.raises(ValueError, match="too frequently"):
        validate_schedule("cron", "* * * * *")


def test_validate_accepts_weekday_cron():
    validate_schedule("cron", "0 9 * * 1-5", "Asia/Singapore")


def test_naive_once_time_resolves_in_configured_zone():
    dt = parse_once_datetime("2026-08-02T09:00:00", "Asia/Singapore")
    assert dt == datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)


def test_once_time_with_offset_is_respected():
    dt = parse_once_datetime("2026-08-02T09:00:00+00:00", "Asia/Singapore")
    assert dt == datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
