"""Job lifecycle state machine.

    booked --start(fundi)--> in_progress --mark_complete(fundi)--> awaiting_confirmation
        awaiting_confirmation --confirm(customer|fundi, with code)--> completed
        booked|in_progress --cancel(customer|fundi|admin)--> cancelled
        booked --report_no_show(customer, >30min after slot)--> no_show

Only ``confirm`` can reach ``completed``, and ``confirm`` requires a valid completion code
(checked in ``completion.py``). The review gate depends on ``completed``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from .models import Job, JobStatus

NO_SHOW_GRACE = timedelta(minutes=30)
ON_TIME_GRACE = timedelta(minutes=15)


class Actor(StrEnum):
    CUSTOMER = "customer"
    FUNDI = "fundi"
    ADMIN = "admin"


class Event(StrEnum):
    START = "start"
    MARK_COMPLETE = "mark_complete"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    REPORT_NO_SHOW = "report_no_show"


class LifecycleError(Exception):
    """Base class for lifecycle violations."""


class InvalidTransition(LifecycleError):
    pass


class NotPermitted(LifecycleError):
    pass


class TooEarly(LifecycleError):
    pass


S = JobStatus
A = Actor
E = Event

TRANSITIONS: dict[tuple[JobStatus, Event], tuple[JobStatus, frozenset[Actor]]] = {
    (S.BOOKED, E.START): (S.IN_PROGRESS, frozenset({A.FUNDI})),
    (S.IN_PROGRESS, E.MARK_COMPLETE): (S.AWAITING_CONFIRMATION, frozenset({A.FUNDI})),
    (S.AWAITING_CONFIRMATION, E.CONFIRM): (S.COMPLETED, frozenset({A.CUSTOMER, A.FUNDI})),
    (S.BOOKED, E.CANCEL): (S.CANCELLED, frozenset({A.CUSTOMER, A.FUNDI, A.ADMIN})),
    (S.IN_PROGRESS, E.CANCEL): (S.CANCELLED, frozenset({A.CUSTOMER, A.FUNDI, A.ADMIN})),
    (S.AWAITING_CONFIRMATION, E.CANCEL): (S.CANCELLED, frozenset({A.ADMIN})),
    (S.BOOKED, E.REPORT_NO_SHOW): (S.NO_SHOW, frozenset({A.CUSTOMER})),
}

TERMINAL = frozenset({S.COMPLETED, S.CANCELLED, S.NO_SHOW})


def allowed_events(status: JobStatus, actor: Actor) -> list[Event]:
    return [ev for (st, ev), (_, actors) in TRANSITIONS.items() if st == status and actor in actors]


def transition(job: Job, event: Event, actor: Actor, now: datetime) -> JobStatus:
    """Apply ``event`` to ``job`` in place (timestamps included) and return the new status."""
    current = JobStatus(job.status)
    entry = TRANSITIONS.get((current, event))
    if entry is None:
        raise InvalidTransition(f"Cannot {event.value} a job that is {current.value}.")
    target, actors = entry
    if actor not in actors:
        raise NotPermitted(f"A {actor.value} cannot {event.value} this job.")
    if event is Event.REPORT_NO_SHOW and now < job.scheduled_for + NO_SHOW_GRACE:
        raise TooEarly("You can report a no-show 30 minutes after the booked time.")
    job.status = target
    if event is Event.START:
        job.started_at = now
    elif event is Event.MARK_COMPLETE:
        job.marked_complete_at = now
    elif event is Event.CONFIRM:
        job.completed_at = now
    elif event is Event.CANCEL:
        job.cancelled_by = actor.value
    return target


def was_on_time(job: Job) -> bool | None:
    """True/False once punctuality is decidable for this job, None otherwise.

    A fundi who started within 15 minutes of the slot is on time. A fundi no-show or a
    fundi cancellation counts against them. Customer cancellations are not the fundi's fault.
    """
    status = JobStatus(job.status)
    if status is S.NO_SHOW:
        return False
    if status is S.CANCELLED:
        return False if job.cancelled_by == Actor.FUNDI.value else None
    if job.started_at is None:
        return None
    return job.started_at <= job.scheduled_for + ON_TIME_GRACE
