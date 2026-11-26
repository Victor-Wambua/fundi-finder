"""Ranking that resists gaming. Pure functions, no database access.

See docs/RANKING.md for the formula, worked examples and failure modes.

    quality   = Bayesian mean of ratings (prior pulled to the platform mean),
                recency-weighted, scaled to 0..1
    score     = 100 * (0.55 quality + 0.15 volume + 0.12 response
                       + 0.10 punctuality + 0.08 verification)

New fundis are *not* given a score bonus (that would break monotonicity and let a
single fake review leapfrog an established fundi). Instead a small share of result
slots is reserved for them in ``order_results`` and labelled in the UI.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

PRIOR_MEAN_MIN = 3.5
PRIOR_MEAN_MAX = 4.5
DEFAULT_PLATFORM_MEAN = 4.2
NEW_FUNDI_JOB_THRESHOLD = 5
_MISSING: object = object()


@dataclass(frozen=True)
class RankParams:
    prior_strength: float = 8.0  # "pseudo-reviews" at the platform mean
    half_life_days: float = 180.0
    recency_floor: float = 0.25
    volume_scale: float = 30.0  # completed jobs for ~63% of the volume score
    response_prior: tuple[float, float] = (0.75, 3.0)  # (rate, pseudo-count)
    punctual_prior: tuple[float, float] = (0.80, 3.0)
    w_quality: float = 0.55
    w_volume: float = 0.15
    w_response: float = 0.12
    w_punctual: float = 0.10
    w_verification: float = 0.08


DEFAULT_PARAMS = RankParams()


@dataclass(frozen=True)
class ReviewSignal:
    rating: int
    age_days: float
    trust: float = 1.0  # 1 = normal, 0.25 = under abuse review, 0 = removed


@dataclass(frozen=True)
class FundiStats:
    reviews: Sequence[ReviewSignal] = ()
    completed_jobs: int = 0
    requests_answerable: int = 0
    requests_answered_in_time: int = 0
    punctual_eligible: int = 0
    punctual_jobs: int = 0
    badge_count: int = 0


@dataclass(frozen=True)
class ScoreBreakdown:
    score: float
    quality_mean: float
    quality: float
    volume: float
    response: float
    punctuality: float
    verification: float
    effective_reviews: float
    components: dict[str, float] = field(default_factory=dict)


def clamp_prior_mean(mean: float) -> float:
    return min(PRIOR_MEAN_MAX, max(PRIOR_MEAN_MIN, mean))


def platform_mean(reviews: Sequence[ReviewSignal]) -> float:
    """Mean rating across the platform, clamped so the prior cannot be pushed around."""
    weighted = [(r.rating, r.trust) for r in reviews if r.trust > 0]
    total = sum(t for _, t in weighted)
    if total == 0:
        return DEFAULT_PLATFORM_MEAN
    return clamp_prior_mean(sum(r * t for r, t in weighted) / total)


def recency_weight(age_days: float, params: RankParams = DEFAULT_PARAMS) -> float:
    age = max(0.0, age_days)
    return max(params.recency_floor, 0.5 ** (age / params.half_life_days))


def bayesian_mean(
    reviews: Sequence[ReviewSignal],
    prior_mean: float,
    params: RankParams = DEFAULT_PARAMS,
) -> tuple[float, float]:
    """Return (mean, effective_review_count) of the recency/trust weighted Bayesian mean."""
    prior = clamp_prior_mean(prior_mean)
    weights = [recency_weight(r.age_days, params) * r.trust for r in reviews]
    total_w = sum(weights)
    weighted_sum = sum(w * r.rating for w, r in zip(weights, reviews, strict=True))
    mean = (params.prior_strength * prior + weighted_sum) / (params.prior_strength + total_w)
    return mean, total_w


def _smoothed(successes: int, trials: int, prior: tuple[float, float]) -> float:
    rate, count = prior
    return (successes + rate * count) / (trials + count)


def score_fundi(
    stats: FundiStats,
    prior_mean: float = DEFAULT_PLATFORM_MEAN,
    params: RankParams = DEFAULT_PARAMS,
) -> ScoreBreakdown:
    mean, effective = bayesian_mean(stats.reviews, prior_mean, params)
    quality = (mean - 1.0) / 4.0
    volume = 1.0 - math.exp(-max(0, stats.completed_jobs) / params.volume_scale)
    response = _smoothed(stats.requests_answered_in_time, stats.requests_answerable, params.response_prior)
    punctuality = _smoothed(stats.punctual_jobs, stats.punctual_eligible, params.punctual_prior)
    verification = min(4, max(0, stats.badge_count)) / 4.0
    score = 100.0 * (
        params.w_quality * quality
        + params.w_volume * volume
        + params.w_response * response
        + params.w_punctual * punctuality
        + params.w_verification * verification
    )
    return ScoreBreakdown(
        score=round(score, 4),
        quality_mean=mean,
        quality=quality,
        volume=volume,
        response=response,
        punctuality=punctuality,
        verification=verification,
        effective_reviews=effective,
        components={
            "Review quality": round(100 * params.w_quality * quality, 1),
            "Completed jobs": round(100 * params.w_volume * volume, 1),
            "Replies in time": round(100 * params.w_response * response, 1),
            "Turns up on time": round(100 * params.w_punctual * punctuality, 1),
            "Verification": round(100 * params.w_verification * verification, 1),
        },
    )


def wilson_lower_bound(positive: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a proportion (0 when total == 0)."""
    if total <= 0:
        return 0.0
    p = positive / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


def is_new_fundi(completed_jobs: int) -> bool:
    return completed_jobs < NEW_FUNDI_JOB_THRESHOLD


def order_results[T](
    items: Sequence[T],
    score: Callable[[T], float],
    boost_eligible: Callable[[T], bool],
    day_seed: int = 0,
    every: int = 4,
) -> list[T]:
    """Order by score, but reserve every ``every``-th slot for boost-eligible newcomers.

    Established fundis keep their score order. Newcomers (already filtered by the caller
    to be phone-verified) fill the reserved slots in a daily-rotating order, so exposure is
    shared between them instead of always going to whoever was first.
    """
    ordered = sorted(items, key=score, reverse=True)
    newcomers = [i for i in ordered if boost_eligible(i)]
    established = [i for i in ordered if not boost_eligible(i)]
    if newcomers:
        shift = day_seed % len(newcomers)
        newcomers = newcomers[shift:] + newcomers[:shift]
    result: list[T] = []
    n_iter = iter(newcomers)
    e_iter = iter(established)
    position = 1
    while True:
        slot_for_newcomer = position % every == 0
        first, second = (n_iter, e_iter) if slot_for_newcomer else (e_iter, n_iter)
        nxt = next(first, _MISSING)
        if nxt is _MISSING:
            nxt = next(second, _MISSING)
            if nxt is _MISSING:
                break
        result.append(nxt)  # type: ignore[arg-type]
        position += 1
    return result
