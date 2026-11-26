"""A tiny, overridable clock so time-dependent rules are testable.

All timestamps in the database are naive UTC datetimes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

_frozen: datetime | None = None


def now() -> datetime:
    """Current naive-UTC time (or the frozen time inside ``frozen``)."""
    if _frozen is not None:
        return _frozen
    return datetime.now(UTC).replace(tzinfo=None)


@contextmanager
def frozen(at: datetime) -> Iterator[None]:
    global _frozen
    previous = _frozen
    _frozen = at
    try:
        yield
    finally:
        _frozen = previous


def set_frozen(at: datetime | None) -> None:
    global _frozen
    _frozen = at


def days_ago(days: float, base: datetime | None = None) -> datetime:
    return (base or now()) - timedelta(days=days)
