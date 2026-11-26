"""Run the abuse heuristics over the database and record flags for the admin queue."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, clock
from .abuse import Finding, ReviewerRecord, detect_all
from .models import (
    AbuseFlag,
    FlagStatus,
    FundiProfile,
    Report,
    ReportStatus,
    Review,
    ReviewStatus,
    Role,
    User,
)
from .phones import phone_prefix

DUPLICATE_ACCOUNTS_PER_NETWORK = 4


def reviewer_records(db: Session) -> list[ReviewerRecord]:
    counts = dict(
        db.execute(
            select(Review.customer_id, func.count(Review.id)).group_by(Review.customer_id)
        ).all()
    )
    rows = db.execute(
        select(Review, User, FundiProfile, FundiProfile.user_id)
        .join(User, User.id == Review.customer_id)
        .join(FundiProfile, FundiProfile.id == Review.fundi_id)
        .where(Review.status == ReviewStatus.PUBLISHED)
    ).all()
    fundi_users = {u.id: u for u in db.scalars(select(User).where(User.role == Role.FUNDI))}
    out: list[ReviewerRecord] = []
    for review, customer, _profile, fundi_user_id in rows:
        fu = fundi_users[fundi_user_id]
        out.append(
            ReviewerRecord(
                review_id=review.id,
                user_id=customer.id,
                fundi_id=review.fundi_id,
                rating=review.rating,
                review_created=review.created_at,
                account_created=customer.created_at,
                ip_hash=customer.signup_ip_hash,
                phone_prefix=phone_prefix(customer.phone),
                reviewer_review_count=counts.get(customer.id, 1),
                fundi_account_created=fu.created_at,
                fundi_ip_hash=fu.signup_ip_hash,
                fundi_phone_prefix=phone_prefix(fu.phone),
            )
        )
    return out


def scan(db: Session, actor: User | None = None) -> list[AbuseFlag]:
    """Create flags for findings that are not already known. Returns only the new flags."""
    created: list[AbuseFlag] = []
    for finding in detect_all(reviewer_records(db)):
        flag = _record(db, finding)
        if flag is not None:
            created.append(flag)
    created.extend(_duplicate_account_flags(db))
    if created:
        audit.log(db, actor, "abuse.scan", detail=f"{len(created)} new flag(s)")
    return created


def _record(db: Session, finding: Finding) -> AbuseFlag | None:
    if db.scalar(select(AbuseFlag.id).where(AbuseFlag.signature == finding.signature)):
        return None
    flag = AbuseFlag(
        kind=finding.kind,
        fundi_id=finding.fundi_id,
        score=finding.score,
        summary=finding.summary,
        user_ids=",".join(map(str, finding.user_ids)),
        review_ids=",".join(map(str, finding.review_ids)),
        signature=finding.signature,
        created_at=clock.now(),
    )
    db.add(flag)
    return flag


def _duplicate_account_flags(db: Session) -> list[AbuseFlag]:
    """Many accounts from one sign-up network inside 24 hours."""
    import hashlib

    by_net: dict[str, list[User]] = {}
    for user in db.scalars(select(User).where(User.signup_ip_hash.is_not(None))):
        by_net.setdefault(str(user.signup_ip_hash), []).append(user)
    flags: list[AbuseFlag] = []
    for net, users in by_net.items():
        users.sort(key=lambda u: u.created_at)
        for i, first in enumerate(users):
            burst = [
                u for u in users[i:] if (u.created_at - first.created_at).total_seconds() < 86400
            ]
            if len(burst) < DUPLICATE_ACCOUNTS_PER_NETWORK:
                continue
            ids = sorted(u.id for u in burst)
            sig = hashlib.sha256(f"dup:{net}:{ids}".encode()).hexdigest()[:32]
            if db.scalar(select(AbuseFlag.id).where(AbuseFlag.signature == sig)):
                break
            flag = AbuseFlag(
                kind="duplicate_account",
                score=min(100, 30 + 10 * len(burst)),
                summary=f"{len(burst)} accounts created from one network within 24 hours",
                user_ids=",".join(map(str, ids)),
                signature=sig,
                created_at=clock.now(),
            )
            db.add(flag)
            flags.append(flag)
            break
    return flags


def resolve_flag(db: Session, admin: User, flag: AbuseFlag, confirm: bool) -> None:
    """Confirming removes the flagged reviews and suspends the linked accounts."""
    from .marketplace import RuleViolation

    if admin.role != Role.ADMIN:
        raise RuleViolation("Only admins can resolve abuse flags.")
    if flag.status != FlagStatus.OPEN:
        raise RuleViolation("This flag is already resolved.")
    flag.status = FlagStatus.CONFIRMED if confirm else FlagStatus.DISMISSED
    flag.resolved_by_id = admin.id
    flag.resolved_at = clock.now()
    if confirm:
        for rid in flag.review_id_list:
            review = db.get(Review, rid)
            if review is not None:
                review.status = ReviewStatus.REMOVED
        for uid in flag.user_id_list:
            user = db.get(User, uid)
            if user is not None and user.role == Role.CUSTOMER:
                user.is_suspended = True
                user.suspended_reason = "Linked to a confirmed abuse pattern"
    audit.log(
        db,
        admin,
        "abuse.confirmed" if confirm else "abuse.dismissed",
        "abuse_flag",
        flag.id,
        flag.summary,
    )


def open_report_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Report).where(Report.status == ReportStatus.OPEN)
        )
        or 0
    )
