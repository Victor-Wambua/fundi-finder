"""Reviews: gated by a confirmed job, one per job, editable for 14 days, fundi can reply."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from . import clock
from .marketplace import RuleViolation
from .models import Job, JobStatus, Review, ReviewStatus, User

EDIT_WINDOW = timedelta(days=14)
REVIEW_WINDOW = timedelta(days=30)
MAX_REVIEW_CHARS = 1500
MAX_REPLY_CHARS = 1000


def review_block_reason(db: Session, user: User, job: Job) -> str | None:
    """Why ``user`` cannot review ``job`` right now, or None if they can."""
    if user.id != job.customer_id:
        return "Only the customer who booked the job can review it."
    if job.status != JobStatus.COMPLETED or job.code_used_at is None or job.completed_at is None:
        return "You can review once the job is complete and confirmed with your completion code."
    if job.review is not None:
        return "You have already reviewed this job."
    if clock.now() - job.completed_at > REVIEW_WINDOW:
        return "The 30-day window to review this job has closed."
    return None


def create_review(db: Session, user: User, job: Job, rating: int, body: str) -> Review:
    reason = review_block_reason(db, user, job)
    if reason:
        raise RuleViolation(reason)
    if not 1 <= rating <= 5:
        raise RuleViolation("Choose a rating from 1 to 5.")
    review = Review(
        job_id=job.id,
        customer_id=user.id,
        fundi_id=job.fundi_id,
        rating=rating,
        body=body[:MAX_REVIEW_CHARS],
        created_at=clock.now(),
    )
    db.add(review)
    db.flush()
    return review


def edit_review(db: Session, user: User, review: Review, rating: int, body: str) -> None:
    if user.id != review.customer_id:
        raise RuleViolation("Only the author can edit a review.")
    if review.status != ReviewStatus.PUBLISHED:
        raise RuleViolation("This review has been removed.")
    if clock.now() - review.created_at > EDIT_WINDOW:
        raise RuleViolation("Reviews cannot be edited after 14 days.")
    if not 1 <= rating <= 5:
        raise RuleViolation("Choose a rating from 1 to 5.")
    review.rating = rating
    review.body = body[:MAX_REVIEW_CHARS]
    review.edited_at = clock.now()


def reply_to_review(db: Session, user: User, review: Review, body: str) -> None:
    profile = user.fundi_profile
    if profile is None or profile.id != review.fundi_id:
        raise RuleViolation("Only the reviewed fundi can reply.")
    if review.status != ReviewStatus.PUBLISHED:
        raise RuleViolation("This review has been removed.")
    if review.reply_body:
        raise RuleViolation("You have already replied to this review.")
    if not body.strip():
        raise RuleViolation("Write a reply first.")
    review.reply_body = body.strip()[:MAX_REPLY_CHARS]
    review.reply_at = clock.now()
