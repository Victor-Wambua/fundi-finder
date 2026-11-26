"""Report -> takedown -> appeal. Every decision is written to the audit log."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, clock
from .marketplace import RuleViolation
from .models import (
    AppealStatus,
    Report,
    ReportStatus,
    Review,
    ReviewStatus,
    Role,
    User,
)

REASONS = {
    "fake": "Looks fake or paid for",
    "not_customer": "Reviewer was not a real customer of this fundi",
    "abusive": "Abusive or discriminatory language",
    "personal": "Shares personal information",
    "other": "Something else",
}
APPEAL_WINDOW = timedelta(days=14)


def create_report(
    db: Session, reporter: User, review: Review, target: str, reason: str, detail: str
) -> Report:
    if reason not in REASONS:
        raise RuleViolation("Choose a reason.")
    if target not in ("review", "reply"):
        raise RuleViolation("Unknown report target.")
    if target == "review" and reporter.id == review.customer_id:
        raise RuleViolation("You cannot report your own review.")
    if target == "reply" and (
        reporter.fundi_profile is not None and reporter.fundi_profile.id == review.fundi_id
    ):
        raise RuleViolation("You cannot report your own reply.")
    if target == "reply" and not review.reply_body:
        raise RuleViolation("There is no reply to report.")
    if review.status != ReviewStatus.PUBLISHED:
        raise RuleViolation("This review has already been removed.")
    existing = db.scalar(
        select(Report).where(
            Report.reporter_id == reporter.id,
            Report.review_id == review.id,
            Report.target == target,
            Report.status == ReportStatus.OPEN,
        )
    )
    if existing:
        raise RuleViolation("You have already reported this. A moderator will look at it.")
    report = Report(
        reporter_id=reporter.id,
        review_id=review.id,
        target=target,
        reason=reason,
        detail=detail[:500],
        created_at=clock.now(),
    )
    db.add(report)
    db.flush()
    return report


def author_of(report: Report) -> User:
    """The party whose content was reported (and who may appeal a takedown)."""
    if report.target == "review":
        return report.review.customer
    return report.review.fundi.user


def resolve_report(db: Session, admin: User, report: Report, uphold: bool, note: str) -> None:
    if admin.role != Role.ADMIN:
        raise RuleViolation("Only admins can resolve reports.")
    if report.status != ReportStatus.OPEN:
        raise RuleViolation("This report is already resolved.")
    now = clock.now()
    report.status = ReportStatus.UPHELD if uphold else ReportStatus.DISMISSED
    report.resolved_by_id = admin.id
    report.resolved_at = now
    report.resolution_note = note[:300]
    if uphold:
        _take_down(report)
    audit.log(
        db,
        admin,
        "report.upheld" if uphold else "report.dismissed",
        "report",
        report.id,
        f"{report.target} #{report.review_id}: {note}",
    )


def _take_down(report: Report) -> None:
    review = report.review
    if report.target == "review":
        review.status = ReviewStatus.REMOVED
    else:
        review.reply_removed = True


def _restore(report: Report) -> None:
    review = report.review
    if report.target == "review":
        review.status = ReviewStatus.PUBLISHED
    else:
        review.reply_removed = False


def submit_appeal(db: Session, user: User, report: Report, text: str) -> None:
    if report.status != ReportStatus.UPHELD:
        raise RuleViolation("Only a takedown can be appealed.")
    if user.id != author_of(report).id:
        raise RuleViolation("Only the author of the removed content can appeal.")
    if report.appeal_status != AppealStatus.NONE:
        raise RuleViolation("You have already appealed this decision.")
    if report.resolved_at is None or clock.now() - report.resolved_at > APPEAL_WINDOW:
        raise RuleViolation("The 14-day appeal window has closed.")
    if len(text.strip()) < 10:
        raise RuleViolation("Tell us why the decision was wrong (at least 10 characters).")
    report.appeal_status = AppealStatus.PENDING
    report.appeal_text = text.strip()[:800]
    report.appeal_at = clock.now()
    audit.log(db, user, "appeal.submitted", "report", report.id)


def decide_appeal(db: Session, admin: User, report: Report, accept: bool, note: str) -> None:
    if admin.role != Role.ADMIN:
        raise RuleViolation("Only admins can decide appeals.")
    if report.appeal_status != AppealStatus.PENDING:
        raise RuleViolation("There is no pending appeal.")
    if admin.id == report.resolved_by_id:
        raise RuleViolation("A different admin must decide the appeal.")
    report.appeal_status = AppealStatus.ACCEPTED if accept else AppealStatus.REJECTED
    report.appeal_decided_by_id = admin.id
    report.appeal_decision_note = note[:300]
    if accept:
        _restore(report)
    audit.log(
        db,
        admin,
        "appeal.accepted" if accept else "appeal.rejected",
        "report",
        report.id,
        note,
    )
