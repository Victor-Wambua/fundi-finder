from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fundifinder.ranking import (
    DEFAULT_PLATFORM_MEAN,
    FundiStats,
    ReviewSignal,
    bayesian_mean,
    clamp_prior_mean,
    is_new_fundi,
    order_results,
    platform_mean,
    recency_weight,
    score_fundi,
    wilson_lower_bound,
)

reviews_st = st.lists(
    st.builds(
        ReviewSignal,
        rating=st.integers(1, 5),
        age_days=st.floats(0, 1500, allow_nan=False),
        trust=st.sampled_from([0.0, 0.25, 1.0]),
    ),
    max_size=80,
)
stats_st = st.builds(
    FundiStats,
    reviews=reviews_st,
    completed_jobs=st.integers(0, 300),
    requests_answerable=st.integers(0, 100),
    requests_answered_in_time=st.integers(0, 100),
    punctual_eligible=st.integers(0, 100),
    punctual_jobs=st.integers(0, 100),
    badge_count=st.integers(0, 4),
).filter(
    lambda s: s.requests_answered_in_time <= s.requests_answerable
    and s.punctual_jobs <= s.punctual_eligible
)


def fresh(rating: int, n: int) -> list[ReviewSignal]:
    return [ReviewSignal(rating, 0.0) for _ in range(n)]


# ---- worked examples (the same numbers appear in docs/RANKING.md) ------------------------


def test_no_reviews_means_quality_equals_the_prior():
    mean, eff = bayesian_mean([], 4.2)
    assert mean == pytest.approx(4.2)
    assert eff == 0


def test_one_five_star_review_barely_moves_the_mean():
    mean, _ = bayesian_mean([ReviewSignal(5, 0)], 4.2)
    assert mean == pytest.approx((8 * 4.2 + 5) / 9)
    assert mean < 4.3


def test_sixty_reviews_of_4_8_dominate_the_prior():
    mean, _ = bayesian_mean(fresh(5, 48) + fresh(4, 12), 4.2)
    assert mean == pytest.approx((8 * 4.2 + 5 * 48 + 4 * 12) / 68)
    assert mean > 4.6


def test_worked_example_scores():
    one = score_fundi(FundiStats(reviews=fresh(5, 1), completed_jobs=1), 4.2)
    many = score_fundi(FundiStats(reviews=fresh(5, 48) + fresh(4, 12), completed_jobs=60), 4.2)
    assert one.score == pytest.approx(55 * (3.2889 / 4) + 15 * (1 - math.exp(-1 / 30))
                                      + 12 * 0.75 + 10 * 0.8 + 0, abs=0.05)
    assert many.score > one.score + 10


def test_score_components_sum_to_score():
    b = score_fundi(FundiStats(reviews=fresh(4, 10), completed_jobs=10, badge_count=2), 4.2)
    assert sum(b.components.values()) == pytest.approx(b.score, abs=0.3)


def test_empty_stats_score_is_deterministic_and_modest():
    a = score_fundi(FundiStats())
    b = score_fundi(FundiStats())
    assert a.score == b.score
    assert 0 < a.score < 70


# ---- the two named properties -----------------------------------------------------------


@given(st.floats(3.0, 4.7), st.integers(0, 2), st.integers(0, 4))
def test_one_5_star_never_outranks_60_at_4_8(prior, bad_resp, badges):
    shared = dict(
        requests_answerable=bad_resp * 10, requests_answered_in_time=bad_resp * 5,
        punctual_eligible=10, punctual_jobs=8, badge_count=badges,
    )
    newcomer = FundiStats(reviews=fresh(5, 1), completed_jobs=1, **shared)
    veteran = FundiStats(reviews=fresh(5, 48) + fresh(4, 12), completed_jobs=60, **shared)
    assert score_fundi(newcomer, prior).score < score_fundi(veteran, prior).score


@given(stats_st, st.floats(3.0, 4.7))
@settings(max_examples=200)
def test_an_extra_5_star_review_never_lowers_the_score(stats, prior):
    before = score_fundi(stats, prior).score
    after = score_fundi(
        FundiStats(
            reviews=[*stats.reviews, ReviewSignal(5, 0.0)],
            completed_jobs=stats.completed_jobs + 1,
            requests_answerable=stats.requests_answerable,
            requests_answered_in_time=stats.requests_answered_in_time,
            punctual_eligible=stats.punctual_eligible,
            punctual_jobs=stats.punctual_jobs,
            badge_count=stats.badge_count,
        ),
        prior,
    ).score
    assert after >= before - 1e-9


# ---- further properties -----------------------------------------------------------------


@given(stats_st, st.floats(0, 10))
def test_score_is_bounded(stats, prior):
    assert 0 <= score_fundi(stats, prior).score <= 100


@given(reviews_st, st.floats(3.5, 4.5))
def test_bayesian_mean_stays_between_1_and_5(reviews, prior):
    mean, _ = bayesian_mean(reviews, prior)
    assert 1 <= mean <= 5


@given(reviews_st, st.floats(3.5, 4.5), st.integers(1, 5))
def test_mean_moves_toward_the_added_rating(reviews, prior, rating):
    before, _ = bayesian_mean(reviews, prior)
    after, _ = bayesian_mean([*reviews, ReviewSignal(rating, 0)], prior)
    if rating >= before:
        assert after >= before - 1e-9
    else:
        assert after <= before + 1e-9


@given(st.integers(1, 5), st.integers(0, 40))
def test_quarantined_reviews_have_no_effect(rating, n):
    base = fresh(4, 5)
    noisy = base + [ReviewSignal(rating, 0, trust=0.0) for _ in range(n)]
    assert bayesian_mean(noisy, 4.2)[0] == pytest.approx(bayesian_mean(base, 4.2)[0])


@given(st.integers(1, 40), st.floats(0, 1500))
def test_older_reviews_of_the_same_rating_never_count_more(n, age):
    assert recency_weight(age + 100) <= recency_weight(age)


@given(st.integers(0, 200), st.integers(0, 200), st.integers(1, 200))
def test_wilson_lower_bound_is_a_bound(pos, extra, total):
    positive = min(pos, total)
    lb = wilson_lower_bound(positive, total)
    assert 0 <= lb <= positive / total + 1e-9


def test_recency_weight_halves_each_half_life_down_to_the_floor():
    assert recency_weight(0) == 1
    assert recency_weight(180) == pytest.approx(0.5)
    assert recency_weight(360) == pytest.approx(0.25)
    assert recency_weight(2000) == 0.25
    assert recency_weight(-5) == 1


def test_old_reviews_still_count_a_little():
    old = [ReviewSignal(5, 3000) for _ in range(20)]
    mean, eff = bayesian_mean(old, 4.0)
    assert eff == pytest.approx(5.0)
    assert mean > 4.0


def test_recent_reviews_outweigh_old_ones():
    recent_bad = [ReviewSignal(2, 5) for _ in range(10)]
    old_good = [ReviewSignal(5, 700) for _ in range(10)]
    mixed, _ = bayesian_mean(recent_bad + old_good, 4.2)
    only_old, _ = bayesian_mean(old_good, 4.2)
    assert mixed < only_old


def test_flagged_reviews_count_a_quarter():
    flagged = [ReviewSignal(5, 0, trust=0.25) for _ in range(8)]
    normal = fresh(5, 2)
    assert bayesian_mean(flagged, 4.2)[1] == pytest.approx(bayesian_mean(normal, 4.2)[1])


def test_wilson_examples():
    assert wilson_lower_bound(0, 0) == 0
    assert wilson_lower_bound(3, 3) == pytest.approx(0.4385, abs=0.001)
    assert wilson_lower_bound(58, 60) == pytest.approx(0.886, abs=0.005)
    assert wilson_lower_bound(3, 3) < wilson_lower_bound(58, 60)


def test_platform_mean_is_clamped_and_ignores_quarantined():
    assert platform_mean([]) == DEFAULT_PLATFORM_MEAN
    assert platform_mean(fresh(5, 10)) == 4.5
    assert platform_mean(fresh(1, 10)) == 3.5
    assert platform_mean([ReviewSignal(1, 0, trust=0.0), *fresh(4, 2)]) == 4.0
    assert clamp_prior_mean(9) == 4.5


def test_response_and_punctuality_are_smoothed():
    cold = score_fundi(FundiStats())
    one_late = score_fundi(FundiStats(punctual_eligible=1, punctual_jobs=0))
    assert cold.punctuality == pytest.approx(0.8)
    assert one_late.punctuality == pytest.approx(0.6)


def test_new_fundi_threshold():
    assert is_new_fundi(0) and is_new_fundi(4)
    assert not is_new_fundi(5)


# ---- reserved slots for newcomers -------------------------------------------------------


def _items():
    return [(f"est{i}", 100 - i, False) for i in range(10)] + [(f"new{i}", 50 - i, True) for i in range(3)]


def test_every_fourth_slot_goes_to_a_newcomer():
    out = order_results(_items(), lambda x: x[1], lambda x: x[2])
    assert [i for i, x in enumerate(out) if x[2]] == [3, 7, 11]


def test_established_order_is_preserved():
    out = order_results(_items(), lambda x: x[1], lambda x: x[2])
    est = [x[0] for x in out if not x[2]]
    assert est == [f"est{i}" for i in range(10)]


def test_newcomers_rotate_with_the_day_seed():
    first = [x[0] for x in order_results(_items(), lambda x: x[1], lambda x: x[2], day_seed=0) if x[2]]
    second = [x[0] for x in order_results(_items(), lambda x: x[1], lambda x: x[2], day_seed=1) if x[2]]
    assert first != second and sorted(first) == sorted(second)


def test_only_newcomers_still_returns_everything():
    items = [(f"n{i}", i, True) for i in range(5)]
    assert len(order_results(items, lambda x: x[1], lambda x: x[2])) == 5


def test_empty_input():
    assert order_results([], lambda x: 0, lambda x: False) == []


@given(st.lists(st.tuples(st.integers(0, 100), st.booleans()), max_size=40), st.integers(0, 365))
def test_ordering_is_a_permutation_and_keeps_established_sorted(items, seed):
    tagged = [(i, s, b) for i, (s, b) in enumerate(items)]
    out = order_results(tagged, lambda x: x[1], lambda x: x[2], day_seed=seed)
    assert sorted(out) == sorted(tagged)
    est = [x[1] for x in out if not x[2]]
    assert est == sorted(est, reverse=True)
