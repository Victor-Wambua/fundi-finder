"""Weekly availability pattern plus single-day exceptions. Pure functions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class WeeklyRule:
    weekday: int  # 0 = Monday
    start_hour: int
    end_hour: int


@dataclass(frozen=True)
class DayException:
    day: date
    available: bool


def is_available_on(
    day: date, rules: Iterable[WeeklyRule], exceptions: Iterable[DayException]
) -> bool:
    """An exception always wins; otherwise the weekly rule decides."""
    for ex in exceptions:
        if ex.day == day:
            return ex.available
    return any(r.weekday == day.weekday() and r.end_hour > r.start_hour for r in rules)


def available_days(
    start: date, count: int, rules: list[WeeklyRule], exceptions: list[DayException]
) -> list[date]:
    days = (start + timedelta(days=i) for i in range(count))
    return [d for d in days if is_available_on(d, rules, exceptions)]


def available_this_week(
    today: date, rules: list[WeeklyRule], exceptions: list[DayException]
) -> bool:
    return bool(available_days(today, 7, rules, exceptions))


def summarise(rules: list[WeeklyRule]) -> str:
    """'Mon-Fri 08:00-17:00, Sat 09:00-13:00' style summary."""
    by_hours: dict[tuple[int, int], list[int]] = {}
    for r in sorted(rules, key=lambda r: r.weekday):
        by_hours.setdefault((r.start_hour, r.end_hour), []).append(r.weekday)
    parts = []
    for (start, end), days in by_hours.items():
        parts.append(f"{_runs(days)} {start:02d}:00-{end:02d}:00")
    return ", ".join(parts) if parts else "No regular hours set"


def _runs(days: list[int]) -> str:
    out: list[str] = []
    i = 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and days[j + 1] == days[j] + 1:
            j += 1
        out.append(WEEKDAYS[days[i]] if i == j else f"{WEEKDAYS[days[i]]}-{WEEKDAYS[days[j]]}")
        i = j + 1
    return ", ".join(out)
