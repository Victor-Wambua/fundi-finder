"""Price transparency: what did this kind of job actually cost?"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

MIN_SAMPLE = 5


@dataclass(frozen=True)
class PriceBenchmark:
    median_kes: int
    low_kes: int  # 25th percentile
    high_kes: int  # 75th percentile
    sample_size: int


def benchmark(prices_kes: Sequence[int]) -> PriceBenchmark | None:
    """Median and interquartile range of agreed prices, only when n >= 5.

    Below five jobs a "median" is one customer's private bill; we show nothing instead.
    """
    if len(prices_kes) < MIN_SAMPLE:
        return None
    quartiles = statistics.quantiles(prices_kes, n=4, method="inclusive")
    return PriceBenchmark(
        median_kes=round(statistics.median(prices_kes)),
        low_kes=round(quartiles[0]),
        high_kes=round(quartiles[2]),
        sample_size=len(prices_kes),
    )


def format_kes(amount: int) -> str:
    return f"KES {amount:,}"
