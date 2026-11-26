"""Database models. Money is integer KES; times are naive UTC."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .db import Base


class Role(StrEnum):
    CUSTOMER = "customer"
    FUNDI = "fundi"
    ADMIN = "admin"


class JobStatus(StrEnum):
    BOOKED = "booked"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class RequestStatus(StrEnum):
    OPEN = "open"
    QUOTED = "quoted"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class QuoteStatus(StrEnum):
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class IdStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RefStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


class ReviewStatus(StrEnum):
    PUBLISHED = "published"
    REMOVED = "removed"


class ReportStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class AppealStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class FlagStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200))
    email_canonical: Mapped[str] = mapped_column(String(200), unique=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    signup_ip_hash: Mapped[str | None] = mapped_column(String(32))
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended_reason: Mapped[str | None] = mapped_column(String(200))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    fundi_profile: Mapped[FundiProfile | None] = relationship(back_populates="user", uselist=False)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_fundi(self) -> bool:
        return self.role == Role.FUNDI

    @property
    def is_customer(self) -> bool:
        return self.role == Role.CUSTOMER

    @property
    def first_name(self) -> str:
        return self.name.split(" ")[0]


class Area(Base):
    __tablename__ = "areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    city: Mapped[str] = mapped_column(String(80), default="Nairobi")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    plural: Mapped[str] = mapped_column(String(60))

    job_types: Mapped[list[JobType]] = relationship(back_populates="trade")


class JobType(Base):
    __tablename__ = "job_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(100))

    trade: Mapped[Trade] = relationship(back_populates="job_types")


fundi_areas = Table(
    "fundi_areas",
    Base.metadata,
    Column("fundi_id", ForeignKey("fundi_profiles.id"), primary_key=True),
    Column("area_id", ForeignKey("areas.id"), primary_key=True),
)


class FundiProfile(Base):
    __tablename__ = "fundi_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    headline: Mapped[str] = mapped_column(String(140), default="")
    bio: Mapped[str] = mapped_column(Text, default="")
    years_experience: Mapped[int] = mapped_column(Integer, default=0)
    whatsapp: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    user: Mapped[User] = relationship(back_populates="fundi_profile")
    trade: Mapped[Trade] = relationship()
    areas: Mapped[list[Area]] = relationship(secondary=fundi_areas)
    services: Mapped[list[FundiService]] = relationship(
        back_populates="fundi", cascade="all, delete-orphan", order_by="FundiService.id"
    )
    photos: Mapped[list[PortfolioPhoto]] = relationship(
        back_populates="fundi", cascade="all, delete-orphan", order_by="PortfolioPhoto.id"
    )


class FundiService(Base):
    __tablename__ = "fundi_services"
    __table_args__ = (UniqueConstraint("fundi_id", "job_type_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    job_type_id: Mapped[int] = mapped_column(ForeignKey("job_types.id"))
    price_min_kes: Mapped[int] = mapped_column(Integer)
    price_max_kes: Mapped[int] = mapped_column(Integer)

    fundi: Mapped[FundiProfile] = relationship(back_populates="services")
    job_type: Mapped[JobType] = relationship()


class AvailabilityRule(Base):
    """Weekly pattern: the fundi works ``weekday`` (0=Mon) between the two hours."""

    __tablename__ = "availability_rules"
    __table_args__ = (UniqueConstraint("fundi_id", "weekday"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)


class AvailabilityException(Base):
    """A single date that overrides the weekly pattern (day off, or extra working day)."""

    __tablename__ = "availability_exceptions"
    __table_args__ = (UniqueConstraint("fundi_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    available: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(String(120), default="")


class PortfolioPhoto(Base):
    __tablename__ = "portfolio_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    storage_name: Mapped[str] = mapped_column(String(64))
    caption: Mapped[str] = mapped_column(String(140), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    fundi: Mapped[FundiProfile] = relationship(back_populates="photos")


class PhoneOtp(Base):
    __tablename__ = "phone_otps"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class IdDocument(Base):
    """An uploaded identity document. The file lives in private storage only."""

    __tablename__ = "id_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    storage_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=IdStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str] = mapped_column(String(300), default="")

    fundi: Mapped[FundiProfile] = relationship()


class Reference(Base):
    __tablename__ = "references"

    id: Mapped[int] = mapped_column(primary_key=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(16))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    relationship_note: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default=RefStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    fundi: Mapped[FundiProfile] = relationship()


class QuoteRequest(Base):
    __tablename__ = "quote_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    job_type_id: Mapped[int] = mapped_column(ForeignKey("job_types.id"))
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id"))
    description: Mapped[str] = mapped_column(Text)
    photo_name: Mapped[str | None] = mapped_column(String(64))
    preferred_time: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=RequestStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime)

    customer: Mapped[User] = relationship()
    fundi: Mapped[FundiProfile] = relationship()
    job_type: Mapped[JobType] = relationship()
    area: Mapped[Area] = relationship()
    quotes: Mapped[list[Quote]] = relationship(
        back_populates="request", order_by="Quote.id", cascade="all, delete-orphan"
    )


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("quote_requests.id"), index=True)
    amount_kes: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, default="")
    proposed_start: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=QuoteStatus.SENT)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    request: Mapped[QuoteRequest] = relationship(back_populates="quotes")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("quote_requests.id"), unique=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    job_type_id: Mapped[int] = mapped_column(ForeignKey("job_types.id"), index=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id"))
    price_kes: Mapped[int] = mapped_column(Integer)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default=JobStatus.BOOKED, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    marked_complete_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_by: Mapped[str | None] = mapped_column(String(16))
    cancel_reason: Mapped[str] = mapped_column(String(200), default="")
    # Completion code: derived from ``code_salt`` + server secret, never stored in clear.
    code_salt: Mapped[str] = mapped_column(String(32))
    code_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    code_confirmed_by: Mapped[str | None] = mapped_column(String(16))
    code_failures: Mapped[int] = mapped_column(Integer, default=0)
    code_total_failures: Mapped[int] = mapped_column(Integer, default=0)
    code_locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    code_invalidated: Mapped[bool] = mapped_column(Boolean, default=False)

    request: Mapped[QuoteRequest] = relationship()
    customer: Mapped[User] = relationship()
    fundi: Mapped[FundiProfile] = relationship()
    job_type: Mapped[JobType] = relationship()
    area: Mapped[Area] = relationship()
    review: Mapped[Review | None] = relationship(back_populates="job", uselist=False)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fundi_id: Mapped[int] = mapped_column(ForeignKey("fundi_profiles.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.PUBLISHED, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_body: Mapped[str | None] = mapped_column(Text)
    reply_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_removed: Mapped[bool] = mapped_column(Boolean, default=False)

    job: Mapped[Job] = relationship(back_populates="review")
    customer: Mapped[User] = relationship()
    fundi: Mapped[FundiProfile] = relationship()


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), index=True)
    target: Mapped[str] = mapped_column(String(8), default="review")  # review | reply
    reason: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default=ReportStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_note: Mapped[str] = mapped_column(String(300), default="")
    appeal_status: Mapped[str] = mapped_column(String(16), default=AppealStatus.NONE)
    appeal_text: Mapped[str] = mapped_column(String(800), default="")
    appeal_at: Mapped[datetime | None] = mapped_column(DateTime)
    appeal_decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    appeal_decision_note: Mapped[str] = mapped_column(String(300), default="")

    review: Mapped[Review] = relationship()
    reporter: Mapped[User] = relationship(foreign_keys=[reporter_id])


class AbuseFlag(Base):
    __tablename__ = "abuse_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # review_ring | duplicate_account
    fundi_id: Mapped[int | None] = mapped_column(ForeignKey("fundi_profiles.id"))
    score: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(String(300))
    user_ids: Mapped[str] = mapped_column(String(400), default="")  # comma separated
    review_ids: Mapped[str] = mapped_column(String(400), default="")
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=FlagStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    fundi: Mapped[FundiProfile | None] = relationship()

    @property
    def user_id_list(self) -> list[int]:
        return [int(x) for x in self.user_ids.split(",") if x]

    @property
    def review_id_list(self) -> list[int]:
        return [int(x) for x in self.review_ids.split(",") if x]


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    actor: Mapped[User | None] = relationship()
