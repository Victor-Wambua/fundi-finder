"""Direct-to-database builders for tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from fundifinder import clock
from fundifinder.completion import derive_code, new_salt
from fundifinder.models import (
    Area,
    FundiProfile,
    FundiService,
    Job,
    JobStatus,
    JobType,
    Quote,
    QuoteRequest,
    QuoteStatus,
    RequestStatus,
    Review,
    Role,
    Trade,
    User,
)
from fundifinder.security import hash_password

PASSWORD = "pw-test-1234"
_FAST = "pbkdf2:sha256:1000"
_PW_HASH = hash_password(PASSWORD, _FAST)


@dataclass
class Catalog:
    trade: Trade
    job_type: JobType
    area: Area
    area2: Area


def make_catalog(db: Session) -> Catalog:
    trade = Trade(slug="plumber", name="Plumber", plural="Plumbers")
    other = Trade(slug="tailor", name="Tailor", plural="Tailors")
    db.add_all([trade, other])
    db.flush()
    jt = JobType(trade_id=trade.id, slug="plumber-1", name="Fix a leaking tap")
    db.add(jt)
    a1, a2 = Area(name="Kilimani", slug="kilimani"), Area(name="Westlands", slug="westlands")
    db.add_all([a1, a2])
    db.flush()
    return Catalog(trade, jt, a1, a2)


def make_user(
    db: Session, n: int, role: str = Role.CUSTOMER, *, verified: bool = True,
    created: datetime | None = None, ip: str | None = None, phone: str | None = None,
    name: str | None = None,
) -> User:
    created = created or clock.now() - timedelta(days=100)
    email = f"user{n}@example.test"
    user = User(
        role=role, name=name or f"Test User{n}", email=email, email_canonical=email,
        phone=phone or f"+2547000{n:05d}", password_hash=_PW_HASH, created_at=created,
        signup_ip_hash=ip or f"ip{n:013d}", phone_verified_at=created if verified else None,
    )
    db.add(user)
    db.flush()
    return user


def make_fundi(db: Session, cat: Catalog, n: int, *, verified: bool = True,
               created: datetime | None = None) -> FundiProfile:
    user = make_user(db, n, Role.FUNDI, verified=verified, created=created)
    profile = FundiProfile(user_id=user.id, trade_id=cat.trade.id, headline="Tidy work",
                           years_experience=5, created_at=user.created_at)
    profile.areas = [cat.area]
    db.add(profile)
    db.flush()
    db.add(FundiService(fundi_id=profile.id, job_type_id=cat.job_type.id,
                        price_min_kes=1000, price_max_kes=3000))
    db.flush()
    return profile


def make_admin(db: Session, n: int = 950) -> User:
    return make_user(db, n, Role.ADMIN)


def make_job(
    db: Session, customer: User, fundi: FundiProfile, cat: Catalog,
    status: str = JobStatus.BOOKED, *, scheduled: datetime | None = None, price: int = 2000,
) -> Job:
    scheduled = scheduled or clock.now() + timedelta(days=2)
    req = QuoteRequest(
        customer_id=customer.id, fundi_id=fundi.id, job_type_id=cat.job_type.id,
        area_id=cat.area.id, description="Tap is leaking badly", preferred_time=scheduled,
        status=RequestStatus.ACCEPTED, created_at=scheduled - timedelta(days=3),
        first_response_at=scheduled - timedelta(days=3) + timedelta(hours=1),
    )
    db.add(req)
    db.flush()
    quote = Quote(request_id=req.id, amount_kes=price, proposed_start=scheduled,
                  status=QuoteStatus.ACCEPTED, created_at=req.first_response_at or scheduled)
    db.add(quote)
    db.flush()
    job = Job(
        request_id=req.id, quote_id=quote.id, customer_id=customer.id, fundi_id=fundi.id,
        job_type_id=cat.job_type.id, area_id=cat.area.id, price_kes=price,
        scheduled_for=scheduled, status=status, created_at=req.created_at,
        code_salt=new_salt(),
    )
    if status in (JobStatus.IN_PROGRESS, JobStatus.AWAITING_CONFIRMATION, JobStatus.COMPLETED):
        job.started_at = scheduled
    if status in (JobStatus.AWAITING_CONFIRMATION, JobStatus.COMPLETED):
        job.marked_complete_at = scheduled + timedelta(hours=2)
    if status == JobStatus.COMPLETED:
        job.completed_at = scheduled + timedelta(hours=3)
        job.code_used_at = job.completed_at
        job.code_confirmed_by = "customer"
    db.add(job)
    db.flush()
    return job


def make_review(db: Session, job: Job, rating: int = 5, body: str = "Good work") -> Review:
    review = Review(job_id=job.id, customer_id=job.customer_id, fundi_id=job.fundi_id,
                    rating=rating, body=body, created_at=(job.completed_at or clock.now()))
    db.add(review)
    db.flush()
    return review


def code_for(job: Job, secret: str = "test-secret") -> str:
    return derive_code(secret, job.code_salt)
