"""Read-side queries: fundi stats, badges, scoring, search."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import clock
from .availability import DayException, WeeklyRule, available_this_week
from .badges import Badge, VerificationFacts, derive_badges
from .lifecycle import was_on_time
from .models import (
    AbuseFlag,
    AvailabilityException,
    AvailabilityRule,
    FlagStatus,
    FundiProfile,
    IdDocument,
    IdStatus,
    Job,
    JobStatus,
    PortfolioPhoto,
    QuoteRequest,
    Reference,
    RefStatus,
    RequestStatus,
    Review,
    ReviewStatus,
    User,
)
from .pricing import PriceBenchmark, benchmark
from .ranking import (
    FundiStats,
    ReviewSignal,
    ScoreBreakdown,
    is_new_fundi,
    order_results,
    platform_mean,
    score_fundi,
)

RESPONSE_WINDOW = timedelta(hours=24)


@dataclass
class FundiCard:
    profile: FundiProfile
    stats: FundiStats
    badges: list[Badge]
    breakdown: ScoreBreakdown
    review_count: int
    raw_average: float | None
    median_response_hours: float | None
    available_this_week: bool
    price_min: int | None
    price_max: int | None
    phone_verified: bool
    is_new: bool
    boosted: bool = False  # filled in by search when it took a reserved newcomer slot
    histogram: dict[int, int] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.breakdown.score

    @property
    def completed_jobs(self) -> int:
        return self.stats.completed_jobs


def open_flag_review_ids(db: Session) -> set[int]:
    ids: set[int] = set()
    for flag in db.scalars(select(AbuseFlag).where(AbuseFlag.status == FlagStatus.OPEN)):
        ids.update(flag.review_id_list)
    return ids


def _review_signals(db: Session, now: datetime) -> list[tuple[int, ReviewSignal]]:
    flagged = open_flag_review_ids(db)
    rows = db.execute(
        select(Review.fundi_id, Review.id, Review.rating, Review.created_at).where(
            Review.status == ReviewStatus.PUBLISHED
        )
    ).all()
    return [
        (
            fid,
            ReviewSignal(
                rating=rating,
                age_days=max(0.0, (now - created).total_seconds() / 86400),
                trust=0.25 if rid in flagged else 1.0,
            ),
        )
        for fid, rid, rating, created in rows
    ]


def platform_prior(db: Session, now: datetime | None = None) -> float:
    now = now or clock.now()
    return platform_mean([s for _, s in _review_signals(db, now)])


def load_cards(
    db: Session, profiles: Sequence[FundiProfile], now: datetime | None = None
) -> list[FundiCard]:
    """Compute stats, badges and scores for ``profiles`` with a handful of bulk queries."""
    now = now or clock.now()
    if not profiles:
        return []
    ids = [p.id for p in profiles]
    all_signals = _review_signals(db, now)
    prior = platform_mean([s for _, s in all_signals])
    signals_by: dict[int, list[ReviewSignal]] = defaultdict(list)
    for fid, sig in all_signals:
        if fid in ids:
            signals_by[fid].append(sig)

    completed: dict[int, int] = defaultdict(int)
    punct_ok: dict[int, int] = defaultdict(int)
    punct_n: dict[int, int] = defaultdict(int)
    for job in db.scalars(select(Job).where(Job.fundi_id.in_(ids))):
        if job.status == JobStatus.COMPLETED:
            completed[job.fundi_id] += 1
        verdict = was_on_time(job)
        if verdict is not None:
            punct_n[job.fundi_id] += 1
            punct_ok[job.fundi_id] += int(verdict)

    answerable: dict[int, int] = defaultdict(int)
    in_time: dict[int, int] = defaultdict(int)
    delays: dict[int, list[float]] = defaultdict(list)
    for fid, created, first, status in db.execute(
        select(
            QuoteRequest.fundi_id,
            QuoteRequest.created_at,
            QuoteRequest.first_response_at,
            QuoteRequest.status,
        ).where(QuoteRequest.fundi_id.in_(ids))
    ):
        if first is not None:
            answerable[fid] += 1
            delay = first - created
            delays[fid].append(delay.total_seconds() / 3600)
            in_time[fid] += int(delay <= RESPONSE_WINDOW)
        elif status != RequestStatus.CANCELLED and now - created >= RESPONSE_WINDOW:
            answerable[fid] += 1  # unanswered past the window

    user_by_profile = {p.id: p.user for p in profiles}
    id_ok = set(
        db.scalars(
            select(IdDocument.fundi_id).where(
                IdDocument.fundi_id.in_(ids), IdDocument.status == IdStatus.APPROVED
            )
        )
    )
    refs: dict[int, list[str]] = defaultdict(list)
    for fid, phone in db.execute(
        select(Reference.fundi_id, Reference.phone).where(
            Reference.fundi_id.in_(ids), Reference.status == RefStatus.CONFIRMED
        )
    ):
        refs[fid].append(phone)
    photos: dict[int, int] = defaultdict(int)
    for (fid,) in db.execute(
        select(PortfolioPhoto.fundi_id).where(PortfolioPhoto.fundi_id.in_(ids))
    ):
        photos[fid] += 1

    rules: dict[int, list[WeeklyRule]] = defaultdict(list)
    for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.fundi_id.in_(ids))):
        rules[r.fundi_id].append(WeeklyRule(r.weekday, r.start_hour, r.end_hour))
    excs: dict[int, list[DayException]] = defaultdict(list)
    for e in db.scalars(
        select(AvailabilityException).where(AvailabilityException.fundi_id.in_(ids))
    ):
        excs[e.fundi_id].append(DayException(e.day, e.available))

    cards: list[FundiCard] = []
    for p in profiles:
        user = user_by_profile[p.id]
        facts = VerificationFacts(
            phone_verified=user.phone_verified_at is not None,
            id_document_approved=p.id in id_ok,
            confirmed_reference_phones=tuple(refs[p.id]),
            own_phone=user.phone,
            portfolio_photo_count=photos[p.id],
        )
        badges = derive_badges(facts)
        sigs = signals_by[p.id]
        stats = FundiStats(
            reviews=tuple(sigs),
            completed_jobs=completed[p.id],
            requests_answerable=answerable[p.id],
            requests_answered_in_time=in_time[p.id],
            punctual_eligible=punct_n[p.id],
            punctual_jobs=punct_ok[p.id],
            badge_count=len(badges),
        )
        hist = dict.fromkeys(range(5, 0, -1), 0)
        for s in sigs:
            hist[s.rating] += 1
        prices = [(s.price_min_kes, s.price_max_kes) for s in p.services]
        cards.append(
            FundiCard(
                profile=p,
                stats=stats,
                badges=badges,
                breakdown=score_fundi(stats, prior),
                review_count=len(sigs),
                raw_average=(sum(s.rating for s in sigs) / len(sigs)) if sigs else None,
                median_response_hours=statistics.median(delays[p.id]) if delays[p.id] else None,
                available_this_week=available_this_week(now.date(), rules[p.id], excs[p.id]),
                price_min=min((lo for lo, _ in prices), default=None),
                price_max=max((hi for _, hi in prices), default=None),
                phone_verified=facts.phone_verified,
                is_new=is_new_fundi(completed[p.id]),
                histogram=hist,
            )
        )
    return cards


@dataclass(frozen=True)
class SearchFilters:
    trade_id: int | None = None
    area_id: int | None = None
    verified_only: bool = False
    available_week: bool = False
    min_price: int | None = None
    max_price: int | None = None


def search_fundis(
    db: Session, filters: SearchFilters, now: datetime | None = None
) -> list[FundiCard]:
    now = now or clock.now()
    stmt = (
        select(FundiProfile)
        .join(User, User.id == FundiProfile.user_id)
        .where(User.is_suspended.is_(False))
        .options(
            selectinload(FundiProfile.user),
            selectinload(FundiProfile.areas),
            selectinload(FundiProfile.services),
            selectinload(FundiProfile.trade),
        )
    )
    if filters.trade_id:
        stmt = stmt.where(FundiProfile.trade_id == filters.trade_id)
    profiles = [
        p
        for p in db.scalars(stmt).unique()
        if p.services
        and p.areas
        and (filters.area_id is None or any(a.id == filters.area_id for a in p.areas))
    ]
    if filters.min_price is not None:
        profiles = [
            p for p in profiles if any(s.price_max_kes >= filters.min_price for s in p.services)
        ]
    if filters.max_price is not None:
        profiles = [
            p for p in profiles if any(s.price_min_kes <= filters.max_price for s in p.services)
        ]
    cards = load_cards(db, profiles, now)
    if filters.verified_only:
        cards = [c for c in cards if len(c.badges) >= 2]
    if filters.available_week:
        cards = [c for c in cards if c.available_this_week]
    ordered = order_results(
        cards,
        score=lambda c: c.score,
        boost_eligible=lambda c: c.is_new and c.phone_verified,
        day_seed=now.toordinal(),
    )
    boosted_positions = {i for i, c in enumerate(ordered) if c.is_new and c.phone_verified}
    for i in boosted_positions:
        ordered[i].boosted = True
    return ordered


def job_type_benchmark(db: Session, job_type_id: int) -> PriceBenchmark | None:
    prices = list(
        db.scalars(
            select(Job.price_kes).where(
                Job.job_type_id == job_type_id, Job.status == JobStatus.COMPLETED
            )
        )
    )
    return benchmark(prices)


def benchmarks_by_job_type(db: Session) -> dict[int, PriceBenchmark]:
    grouped: dict[int, list[int]] = defaultdict(list)
    for jt, price in db.execute(
        select(Job.job_type_id, Job.price_kes).where(Job.status == JobStatus.COMPLETED)
    ):
        grouped[jt].append(price)
    out: dict[int, PriceBenchmark] = {}
    for jt, prices in grouped.items():
        b = benchmark(prices)
        if b:
            out[jt] = b
    return out


def recent_completed_jobs(db: Session, profile_id: int, now: datetime, days: int = 90) -> int:
    since = now - timedelta(days=days)
    return len(
        list(
            db.scalars(
                select(Job.id).where(
                    Job.fundi_id == profile_id,
                    Job.status == JobStatus.COMPLETED,
                    Job.completed_at >= since,
                )
            )
        )
    )
