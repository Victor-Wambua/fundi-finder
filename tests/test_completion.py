from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, SECRET
from factories import code_for, make_catalog, make_fundi, make_job, make_user
from fundifinder import completion
from fundifinder.completion import CodeResult, check_code, derive_code, new_salt, normalise, reissue
from fundifinder.models import JobStatus


@pytest.fixture
def job(db):
    cat = make_catalog(db)
    return make_job(db, make_user(db, 1), make_fundi(db, cat, 2), cat, JobStatus.AWAITING_CONFIRMATION)


def test_code_is_six_digits_and_deterministic():
    salt = new_salt()
    code = derive_code(SECRET, salt)
    assert len(code) == 6 and code.isdigit()
    assert derive_code(SECRET, salt) == code


def test_code_depends_on_salt_and_secret():
    assert derive_code(SECRET, "a" * 32) != derive_code(SECRET, "b" * 32)
    assert derive_code("one", "a" * 32) != derive_code("two", "a" * 32)


def test_salts_are_unique():
    assert len({new_salt() for _ in range(200)}) == 200


def test_normalise_strips_spaces_and_dashes():
    assert normalise(" 482-913 ") == "482913"


def test_right_code_succeeds_and_is_marked_used(job):
    result = check_code(job, code_for(job), SECRET, NOW)
    assert result.result is CodeResult.OK
    assert job.code_used_at == NOW


def test_formatted_code_is_accepted(job):
    code = code_for(job)
    assert check_code(job, f"{code[:3]} {code[3:]}", SECRET, NOW).result is CodeResult.OK


def test_code_is_single_use(job):
    code = code_for(job)
    check_code(job, code, SECRET, NOW)
    assert check_code(job, code, SECRET, NOW).result is CodeResult.USED


def test_wrong_code_counts_down_attempts(job):
    wrong = "000000" if code_for(job) != "000000" else "111111"
    r = check_code(job, wrong, SECRET, NOW)
    assert r.result is CodeResult.WRONG and r.attempts_left == 4
    assert job.code_failures == 1 and job.code_used_at is None


def test_five_wrong_codes_lock_for_an_hour(job):
    wrong = "000000" if code_for(job) != "000000" else "111111"
    for _ in range(4):
        check_code(job, wrong, SECRET, NOW)
    r = check_code(job, wrong, SECRET, NOW)
    assert r.result is CodeResult.LOCKED and r.locked_until == NOW + timedelta(hours=1)


def test_correct_code_is_refused_while_locked(job):
    wrong = "000000" if code_for(job) != "000000" else "111111"
    for _ in range(5):
        check_code(job, wrong, SECRET, NOW)
    assert check_code(job, code_for(job), SECRET, NOW).result is CodeResult.LOCKED
    assert job.code_used_at is None


def test_lock_expires(job):
    wrong = "000000" if code_for(job) != "000000" else "111111"
    for _ in range(5):
        check_code(job, wrong, SECRET, NOW)
    later = NOW + timedelta(hours=1, minutes=1)
    assert check_code(job, code_for(job), SECRET, later).result is CodeResult.OK


def test_ten_total_failures_invalidate_the_code(job):
    wrong = "000000" if code_for(job) != "000000" else "111111"
    t = NOW
    for _ in range(9):
        check_code(job, wrong, SECRET, t)
        t += timedelta(hours=2)
    assert check_code(job, wrong, SECRET, t).result is CodeResult.INVALIDATED
    assert check_code(job, code_for(job), SECRET, t + timedelta(days=9)).result is CodeResult.INVALIDATED


def test_reissue_gives_a_new_code_and_clears_counters(job):
    old = code_for(job)
    job.code_invalidated, job.code_total_failures = True, 10
    reissue(job)
    assert code_for(job) != old
    assert not job.code_invalidated and job.code_total_failures == 0
    assert check_code(job, old, SECRET, NOW).result is CodeResult.WRONG
    assert check_code(job, code_for(job), SECRET, NOW).result is CodeResult.OK


def test_empty_and_garbage_input_is_just_wrong(job):
    for junk in ("", "abc", "12", "9" * 40):
        assert check_code(job, junk, SECRET, NOW).result in (CodeResult.WRONG, CodeResult.LOCKED)


def test_guess_space_is_a_million():
    assert completion.CODE_DIGITS == 6 and completion.MAX_TOTAL_FAILURES == 10
