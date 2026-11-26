from __future__ import annotations

from datetime import date

import pytest

from fundifinder.availability import (
    DayException,
    WeeklyRule,
    available_days,
    available_this_week,
    is_available_on,
    summarise,
)
from fundifinder.forms import Form
from fundifinder.phones import (
    canonical_email,
    display_phone,
    normalise_phone,
    phone_prefix,
    tel_url,
    whatsapp_url,
)
from fundifinder.pricing import MIN_SAMPLE, benchmark, format_kes
from fundifinder.security import RateLimiter, ip_hash, safe_next

# ---- pricing ----------------------------------------------------------------------------


def test_no_benchmark_below_five_jobs():
    assert MIN_SAMPLE == 5
    assert benchmark([1000, 2000, 3000, 4000]) is None
    assert benchmark([]) is None


def test_benchmark_at_five_jobs():
    b = benchmark([1000, 1500, 2000, 2500, 9000])
    assert b is not None and b.median_kes == 2000 and b.sample_size == 5
    assert b.low_kes == 1500 and b.high_kes == 2500


def test_median_is_robust_to_one_outlier():
    assert benchmark([1000, 1100, 1200, 1300, 900000]).median_kes == 1200  # type: ignore[union-attr]


def test_even_sample_median_is_an_integer():
    b = benchmark([1000, 2000, 3000, 4000, 5000, 6001])
    assert b is not None and isinstance(b.median_kes, int)


def test_format_kes():
    assert format_kes(1234567) == "KES 1,234,567"


# ---- availability -----------------------------------------------------------------------

MON = date(2026, 6, 15)  # a Monday
RULES = [WeeklyRule(0, 8, 17), WeeklyRule(2, 8, 17)]


def test_weekly_rule_decides_by_weekday():
    assert is_available_on(MON, RULES, [])
    assert not is_available_on(date(2026, 6, 16), RULES, [])
    assert is_available_on(date(2026, 6, 17), RULES, [])


def test_exception_overrides_rule_both_ways():
    assert not is_available_on(MON, RULES, [DayException(MON, False)])
    assert is_available_on(date(2026, 6, 16), RULES, [DayException(date(2026, 6, 16), True)])


def test_empty_hours_are_not_available():
    assert not is_available_on(MON, [WeeklyRule(0, 9, 9)], [])


def test_available_days_window():
    assert available_days(MON, 7, RULES, []) == [MON, date(2026, 6, 17)]


def test_available_this_week_respects_exceptions():
    off = [DayException(MON, False), DayException(date(2026, 6, 17), False)]
    assert available_this_week(MON, RULES, [])
    assert not available_this_week(MON, RULES, off)
    assert not available_this_week(MON, [], [])


def test_summary_groups_runs():
    rules = [WeeklyRule(d, 8, 17) for d in range(5)] + [WeeklyRule(5, 8, 13)]
    assert summarise(rules) == "Mon-Fri 08:00-17:00, Sat 08:00-13:00"
    assert summarise([]) == "No regular hours set"


# ---- phones and emails ------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0700 000 001", "0700000001", "+254700000001", "254700000001",
                                 "0700-000-001", "(0700) 000 001"])
def test_phone_normalisation(raw):
    assert normalise_phone(raw) == "+254700000001"


@pytest.mark.parametrize("raw", ["", "12345", "0200 000 001", "+1 202 555 0100", "07000000", "abc"])
def test_bad_phones_rejected(raw):
    assert normalise_phone(raw) is None


def test_phone_helpers():
    assert display_phone("+254700000001") == "0700 000 001"
    assert phone_prefix("+254700000905") == "+2547000009"
    assert whatsapp_url("+254700000001") == "https://wa.me/254700000001"
    assert whatsapp_url("+254700000001", "Hi there").endswith("?text=Hi%20there")
    assert tel_url("+254700000001") == "tel:+254700000001"


def test_canonical_email():
    assert canonical_email("Jo.Hn+spam@Gmail.com") == "john@gmail.com"
    assert canonical_email("jo.hn+x@example.test") == "jo.hn@example.test"


# ---- forms ------------------------------------------------------------------------------


def test_form_collects_errors():
    f = Form({"name": "A", "email": "nope", "phone": "123", "n": "x", "d": "bad"})
    f.text("name", "Name", 2, 10)
    f.email()
    f.phone()
    f.integer("n", "Count", 1, 5)
    f.datetime_local("d", "date")
    f.choice("c", "colour", {"red"})
    assert set(f.errors) == {"name", "email", "phone", "n", "d", "c"} and not f.ok


def test_form_accepts_good_input():
    f = Form({"name": "Amos", "email": "A@B.co", "n": "1,200", "c": "red", "d": "2026-06-15T10:00"})
    assert f.text("name", "Name", 2, 10) == "Amos"
    assert f.email() == "a@b.co"
    assert f.integer("n", "Count", 1, 5000) == 1200
    assert f.choice("c", "colour", {"red"}) == "red"
    assert f.datetime_local("d", "date") is not None
    assert f.ok


def test_integer_bounds_and_optional():
    f = Form({"n": "99"})
    assert f.integer("n", "Count", 1, 5) is None and "n" in f.errors
    f2 = Form({})
    assert f2.integer("n", "Count", 1, 5, required=False) is None and f2.ok


# ---- security helpers -------------------------------------------------------------------


def test_rate_limiter_blocks_after_limit_and_recovers():
    from datetime import timedelta

    from conftest import NOW
    from fundifinder import clock

    rl = RateLimiter()
    assert [rl.check("k", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert rl.check("k", 3, 60) > 0
    assert rl.check("other", 3, 60) == 0
    with clock.frozen(NOW + timedelta(seconds=61)):
        assert rl.check("k", 3, 60) == 0


def test_ip_hash_is_keyed_and_stable():
    assert ip_hash("1.2.3.4", "a") == ip_hash("1.2.3.4", "a")
    assert ip_hash("1.2.3.4", "a") != ip_hash("1.2.3.4", "b")
    assert "1.2.3.4" not in ip_hash("1.2.3.4", "a")


@pytest.mark.parametrize(("target", "ok"), [("/me", True), ("/search?trade=x", True), ("//evil.com", False),
                                            ("https://evil.com", False), ("/\\evil", False), (None, False)])
def test_safe_next(target, ok):
    assert (safe_next(target) is not None) == ok
