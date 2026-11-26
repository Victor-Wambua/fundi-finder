"""Customer/fundi workflow: request -> quote -> booking -> on-site completion."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, clock
from .completion import CodeCheck, CodeResult, check_code, new_salt
from .lifecycle import Actor, Event, LifecycleError, transition
from .models import (
    FundiProfile,
    Job,
    JobStatus,
    Quote,
    QuoteRequest,
    QuoteStatus,
    RequestStatus,
    Role,
    User,
)

MIN_QUOTE_KES = 100
MAX_QUOTE_KES = 5_000_000
MAX_OPEN_REQUESTS_PER_CUSTOMER = 10


class RuleViolation(Exception):
    """A business rule stopped the action; the message is safe to show to the user."""


def create_request(
    db: Session,
    customer: User,
    fundi: FundiProfile,
    job_type_id: int,
    area_id: int,
    description: str,
    preferred_time: datetime,
    photo_name: str | None = None,
) -> QuoteRequest:
    now = clock.now()
    if customer.role != Role.CUSTOMER:
        raise RuleViolation("Only customers can request quotes.")
    if customer.phone_verified_at is None:
        raise RuleViolation("Verify your phone number before requesting a quote.")
    if fundi.user.is_suspended:
        raise RuleViolation("This fundi is not taking requests.")
    if preferred_time < now - timedelta(hours=1):
        raise RuleViolation("Choose a time in the future.")
    if job_type_id not in {s.job_type_id for s in fundi.services}:
        raise RuleViolation("This fundi does not offer that job.")
    if area_id not in {a.id for a in fundi.areas}:
        raise RuleViolation("This fundi does not work in that area.")
    open_count = len(
        list(
            db.scalars(
                select(QuoteRequest.id).where(
                    QuoteRequest.customer_id == customer.id,
                    QuoteRequest.status.in_([RequestStatus.OPEN, RequestStatus.QUOTED]),
                )
            )
        )
    )
    if open_count >= MAX_OPEN_REQUESTS_PER_CUSTOMER:
        raise RuleViolation("You have too many open requests. Cancel or finish some first.")
    req = QuoteRequest(
        customer_id=customer.id,
        fundi_id=fundi.id,
        job_type_id=job_type_id,
        area_id=area_id,
        description=description,
        photo_name=photo_name,
        preferred_time=preferred_time,
        created_at=now,
    )
    db.add(req)
    db.flush()
    return req


def _sent_quotes(db: Session, req: QuoteRequest) -> list[Quote]:
    return list(
        db.scalars(
            select(Quote).where(Quote.request_id == req.id, Quote.status == QuoteStatus.SENT)
        )
    )


def send_quote(
    db: Session,
    fundi_user: User,
    req: QuoteRequest,
    amount_kes: int,
    message: str,
    proposed_start: datetime,
) -> Quote:
    now = clock.now()
    if fundi_user.fundi_profile is None or req.fundi_id != fundi_user.fundi_profile.id:
        raise RuleViolation("This request was not sent to you.")
    if fundi_user.phone_verified_at is None:
        raise RuleViolation("Verify your phone number before sending quotes.")
    if req.status not in (RequestStatus.OPEN, RequestStatus.QUOTED):
        raise RuleViolation("This request is closed.")
    if not MIN_QUOTE_KES <= amount_kes <= MAX_QUOTE_KES:
        raise RuleViolation(f"A quote must be between KES {MIN_QUOTE_KES:,} and KES {MAX_QUOTE_KES:,}.")
    if proposed_start < now:
        raise RuleViolation("Propose a start time in the future.")
    for old in _sent_quotes(db, req):
        old.status = QuoteStatus.SUPERSEDED
    quote = Quote(
        request_id=req.id,
        amount_kes=amount_kes,
        message=message,
        proposed_start=proposed_start,
        created_at=now,
    )
    db.add(quote)
    req.status = RequestStatus.QUOTED
    if req.first_response_at is None:
        req.first_response_at = now
    db.flush()
    return quote


def decline_request(db: Session, fundi_user: User, req: QuoteRequest) -> None:
    if fundi_user.fundi_profile is None or req.fundi_id != fundi_user.fundi_profile.id:
        raise RuleViolation("This request was not sent to you.")
    if req.status not in (RequestStatus.OPEN, RequestStatus.QUOTED):
        raise RuleViolation("This request is closed.")
    req.status = RequestStatus.DECLINED
    if req.first_response_at is None:
        req.first_response_at = clock.now()


def cancel_request(db: Session, customer: User, req: QuoteRequest) -> None:
    if req.customer_id != customer.id:
        raise RuleViolation("This is not your request.")
    if req.status not in (RequestStatus.OPEN, RequestStatus.QUOTED):
        raise RuleViolation("This request can no longer be cancelled.")
    req.status = RequestStatus.CANCELLED
    for q in _sent_quotes(db, req):
        q.status = QuoteStatus.REJECTED


def accept_quote(db: Session, customer: User, quote: Quote) -> Job:
    req = quote.request
    if req.customer_id != customer.id:
        raise RuleViolation("This is not your request.")
    if quote.status != QuoteStatus.SENT or req.status != RequestStatus.QUOTED:
        raise RuleViolation("This quote is no longer available.")
    if quote.proposed_start < clock.now():
        raise RuleViolation("This quote's start time has passed. Ask the fundi for a new one.")
    quote.status = QuoteStatus.ACCEPTED
    req.status = RequestStatus.ACCEPTED
    job = Job(
        request_id=req.id,
        quote_id=quote.id,
        customer_id=req.customer_id,
        fundi_id=req.fundi_id,
        job_type_id=req.job_type_id,
        area_id=req.area_id,
        price_kes=quote.amount_kes,
        scheduled_for=quote.proposed_start,
        status=JobStatus.BOOKED,
        created_at=clock.now(),
        code_salt=new_salt(),
    )
    db.add(job)
    db.flush()
    return job


# ---- job actions -----------------------------------------------------------------------


def actor_for(user: User, job: Job) -> Actor:
    if user.role == Role.ADMIN:
        return Actor.ADMIN
    if user.id == job.customer_id:
        return Actor.CUSTOMER
    if user.fundi_profile is not None and user.fundi_profile.id == job.fundi_id:
        return Actor.FUNDI
    raise RuleViolation("You are not part of this job.")


def can_view_job(user: User, job: Job) -> bool:
    try:
        actor_for(user, job)
    except RuleViolation:
        return False
    return True


def can_see_code(user: User, job: Job) -> bool:
    """Only the customer sees the code. The fundi has to be told it on site."""
    return user.id == job.customer_id


def apply_event(db: Session, user: User, job: Job, event: Event, reason: str = "") -> None:
    actor = actor_for(user, job)
    now = clock.now()
    try:
        transition(job, event, actor, now)
    except LifecycleError as exc:
        raise RuleViolation(str(exc)) from exc
    if event is Event.CANCEL:
        job.cancel_reason = reason[:200]
        if actor is Actor.ADMIN:
            audit.log(db, user, "job.cancel", "job", job.id, reason)


def confirm_completion(db: Session, user: User, job: Job, code: str, secret: str) -> CodeCheck:
    """Enter the completion code (customer, or the fundi reading what the customer told them).

    The failure counters are committed even on a wrong code, otherwise the lockout is useless.
    """
    actor = actor_for(user, job)
    if actor is Actor.ADMIN:
        raise RuleViolation("Admins cannot confirm completion.")
    if job.status != JobStatus.AWAITING_CONFIRMATION:
        raise RuleViolation("The fundi has not marked this job complete yet.")
    now = clock.now()
    result = check_code(job, code, secret, now)
    if result.result is CodeResult.OK:
        transition(job, Event.CONFIRM, actor, now)
        job.code_confirmed_by = actor.value
    db.commit()
    return result


def list_requests_for_fundi(db: Session, profile_id: int) -> list[QuoteRequest]:
    return list(
        db.scalars(
            select(QuoteRequest)
            .where(QuoteRequest.fundi_id == profile_id)
            .order_by(QuoteRequest.created_at.desc())
        )
    )
