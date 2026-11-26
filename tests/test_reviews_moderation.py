from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, SECRET
from factories import (
    code_for,
    make_admin,
    make_catalog,
    make_fundi,
    make_job,
    make_review,
    make_user,
)
from fundifinder import clock, marketplace, moderation, reviews
from fundifinder.completion import CodeResult
from fundifinder.marketplace import RuleViolation
from fundifinder.models import AppealStatus, JobStatus, ReportStatus, ReviewStatus


@pytest.fixture
def world(db):
    cat = make_catalog(db)
    customer, other = make_user(db, 1), make_user(db, 3)
    fundi = make_fundi(db, cat, 2)
    return db, cat, customer, other, fundi


# ---- the review gate --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.BOOKED,
        JobStatus.IN_PROGRESS,
        JobStatus.AWAITING_CONFIRMATION,
        JobStatus.CANCELLED,
        JobStatus.NO_SHOW,
    ],
)
def test_cannot_review_a_job_that_is_not_completed(world, status):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, status)
    with pytest.raises(RuleViolation, match="completion code"):
        reviews.create_review(db, customer, job, 5, "great")


def test_cannot_review_without_the_code_even_if_status_says_completed(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    job.code_used_at = None  # a tampered row must still be refused
    with pytest.raises(RuleViolation):
        reviews.create_review(db, customer, job, 5, "great")


def test_cannot_review_after_wrong_codes_only(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    for _ in range(3):
        marketplace.confirm_completion(
            db, customer, job, "000000" if code_for(job) != "000000" else "111111", SECRET
        )
    assert reviews.review_block_reason(db, customer, job) is not None


def test_full_gate_confirm_then_review(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    assert reviews.review_block_reason(db, customer, job) is not None
    result = marketplace.confirm_completion(db, customer, job, code_for(job), SECRET)
    assert result.result is CodeResult.OK and job.status == JobStatus.COMPLETED
    review = reviews.create_review(db, customer, job, 4, "Good")
    assert review.rating == 4 and review.fundi_id == fundi.id


def test_fundi_cannot_review_their_own_job(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    with pytest.raises(RuleViolation, match="Only the customer"):
        reviews.create_review(db, fundi.user, job, 5, "I am great")


def test_other_customers_cannot_review_the_job(world):
    db, cat, customer, other, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    with pytest.raises(RuleViolation):
        reviews.create_review(db, other, job, 1, "spite")


def test_one_review_per_job(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    reviews.create_review(db, customer, job, 5, "ok")
    db.refresh(job)
    with pytest.raises(RuleViolation, match="already reviewed"):
        reviews.create_review(db, customer, job, 5, "again")


def test_rating_must_be_1_to_5(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    for bad in (0, 6, -1):
        with pytest.raises(RuleViolation):
            reviews.create_review(db, customer, job, bad, "")


def test_review_window_closes_after_30_days(world):
    db, cat, customer, _, fundi = world
    job = make_job(
        db, customer, fundi, cat, JobStatus.COMPLETED, scheduled=NOW - timedelta(days=31)
    )
    with pytest.raises(RuleViolation, match="30-day"):
        reviews.create_review(db, customer, job, 5, "late")


# ---- edit window and replies ------------------------------------------------------------


def test_edit_allowed_within_14_days_then_refused(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    review = make_review(db, job, 3)
    review.created_at = NOW - timedelta(days=13)
    reviews.edit_review(db, customer, review, 5, "better than I thought")
    assert review.rating == 5 and review.edited_at == NOW
    review.created_at = NOW - timedelta(days=15)
    with pytest.raises(RuleViolation, match="14 days"):
        reviews.edit_review(db, customer, review, 1, "changed my mind")


def test_only_the_author_can_edit(world):
    db, cat, customer, other, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    with pytest.raises(RuleViolation):
        reviews.edit_review(db, other, review, 1, "x")
    with pytest.raises(RuleViolation):
        reviews.edit_review(db, fundi.user, review, 5, "x")


def test_fundi_can_reply_once(world):
    db, cat, customer, _, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    reviews.reply_to_review(db, fundi.user, review, "Thank you")
    assert review.reply_body == "Thank you"
    with pytest.raises(RuleViolation, match="already replied"):
        reviews.reply_to_review(db, fundi.user, review, "And again")


def test_only_the_reviewed_fundi_can_reply(world):
    db, cat, customer, other, fundi = world
    other_fundi = make_fundi(db, cat, 7)
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    for user in (other, customer, other_fundi.user):
        with pytest.raises(RuleViolation):
            reviews.reply_to_review(db, user, review, "hi")


def test_blank_reply_refused(world):
    db, cat, customer, _, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    with pytest.raises(RuleViolation):
        reviews.reply_to_review(db, fundi.user, review, "   ")


# ---- confirm_completion permissions -----------------------------------------------------


def test_fundi_can_enter_the_code_the_customer_reads_out(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    assert (
        marketplace.confirm_completion(db, fundi.user, job, code_for(job), SECRET).result
        is CodeResult.OK
    )
    assert job.code_confirmed_by == "fundi"


def test_code_cannot_be_entered_before_the_fundi_marks_complete(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.IN_PROGRESS)
    with pytest.raises(RuleViolation):
        marketplace.confirm_completion(db, customer, job, code_for(job), SECRET)


def test_strangers_cannot_confirm_and_only_the_customer_sees_the_code(world):
    db, cat, customer, other, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    with pytest.raises(RuleViolation):
        marketplace.confirm_completion(db, other, job, code_for(job), SECRET)
    assert marketplace.can_see_code(customer, job)
    assert not marketplace.can_see_code(fundi.user, job)
    assert not marketplace.can_view_job(other, job)


def test_wrong_code_counters_survive_the_commit(world):
    db, cat, customer, _, fundi = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    wrong = "000000" if code_for(job) != "000000" else "111111"
    marketplace.confirm_completion(db, customer, job, wrong, SECRET)
    db.expire_all()
    assert job.code_failures == 1


# ---- reports, takedown, appeal ----------------------------------------------------------


@pytest.fixture
def reported(world):
    db, cat, customer, other, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED), 1, "terrible")
    admin, admin2 = make_admin(db, 950), make_admin(db, 951)
    report = moderation.create_report(db, other, review, "review", "abusive", "rude")
    return db, review, report, admin, admin2, customer, fundi


def test_report_requires_a_valid_reason(world):
    db, cat, customer, other, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    with pytest.raises(RuleViolation):
        moderation.create_report(db, other, review, "review", "because", "")


def test_cannot_report_your_own_review_or_reply(world):
    db, cat, customer, _, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    review.reply_body = "thanks"
    with pytest.raises(RuleViolation):
        moderation.create_report(db, customer, review, "review", "fake", "")
    with pytest.raises(RuleViolation):
        moderation.create_report(db, fundi.user, review, "reply", "fake", "")


def test_duplicate_open_reports_refused(reported):
    db, review, report, *_ = reported
    other = report.reporter
    with pytest.raises(RuleViolation, match="already reported"):
        moderation.create_report(db, other, review, "review", "fake", "")


def test_upholding_removes_the_review_and_logs_it(reported):
    db, review, report, admin, *_ = reported
    moderation.resolve_report(db, admin, report, True, "abusive language")
    assert review.status == ReviewStatus.REMOVED and report.status == ReportStatus.UPHELD
    from sqlalchemy import select

    from fundifinder.models import AuditLog

    assert db.scalar(select(AuditLog).where(AuditLog.action == "report.upheld")) is not None


def test_dismissing_keeps_the_review(reported):
    db, review, report, admin, *_ = reported
    moderation.resolve_report(db, admin, report, False, "fine")
    assert review.status == ReviewStatus.PUBLISHED and report.status == ReportStatus.DISMISSED


def test_only_admins_resolve_and_only_once(reported):
    db, review, report, admin, _, customer, _ = reported
    with pytest.raises(RuleViolation):
        moderation.resolve_report(db, customer, report, True, "")
    moderation.resolve_report(db, admin, report, True, "")
    with pytest.raises(RuleViolation):
        moderation.resolve_report(db, admin, report, False, "")


def test_author_can_appeal_once_and_a_different_admin_decides(reported):
    db, review, report, admin, admin2, customer, _ = reported
    moderation.resolve_report(db, admin, report, True, "language")
    moderation.submit_appeal(db, customer, report, "The word described the work, not a person.")
    assert report.appeal_status == AppealStatus.PENDING
    with pytest.raises(RuleViolation, match="already appealed"):
        moderation.submit_appeal(db, customer, report, "second try with more words")
    with pytest.raises(RuleViolation, match="different admin"):
        moderation.decide_appeal(db, admin, report, True, "")
    moderation.decide_appeal(db, admin2, report, True, "fair point")
    assert review.status == ReviewStatus.PUBLISHED and report.appeal_status == AppealStatus.ACCEPTED


def test_rejected_appeal_keeps_the_takedown(reported):
    db, review, report, admin, admin2, customer, _ = reported
    moderation.resolve_report(db, admin, report, True, "")
    moderation.submit_appeal(db, customer, report, "I disagree with this decision.")
    moderation.decide_appeal(db, admin2, report, False, "stands")
    assert review.status == ReviewStatus.REMOVED and report.appeal_status == AppealStatus.REJECTED


def test_only_the_author_can_appeal_and_only_takedowns(reported):
    db, review, report, admin, _, customer, fundi = reported
    with pytest.raises(RuleViolation, match="takedown"):
        moderation.submit_appeal(db, customer, report, "not yet removed at all")
    moderation.resolve_report(db, admin, report, True, "")
    with pytest.raises(RuleViolation, match="author"):
        moderation.submit_appeal(db, fundi.user, report, "I am the fundi, not the author")


def test_appeal_window_is_14_days(reported):
    db, review, report, admin, _, customer, _ = reported
    moderation.resolve_report(db, admin, report, True, "")
    with clock.frozen(NOW + timedelta(days=15)), pytest.raises(RuleViolation, match="14-day"):
        moderation.submit_appeal(db, customer, report, "better late than never, surely")


def test_reply_takedown_hides_only_the_reply(world):
    db, cat, customer, other, fundi = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    review.reply_body = "rude reply"
    admin = make_admin(db)
    report = moderation.create_report(db, other, review, "reply", "abusive", "")
    moderation.resolve_report(db, admin, report, True, "")
    assert review.reply_removed and review.status == ReviewStatus.PUBLISHED
    assert moderation.author_of(report).id == fundi.user.id
