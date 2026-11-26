from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW
from factories import make_catalog, make_fundi, make_job, make_user
from fundifinder.lifecycle import (
    TRANSITIONS,
    Actor,
    Event,
    InvalidTransition,
    NotPermitted,
    TooEarly,
    allowed_events,
    transition,
    was_on_time,
)
from fundifinder.models import JobStatus


@pytest.fixture
def job(db):
    cat = make_catalog(db)
    return make_job(db, make_user(db, 1), make_fundi(db, cat, 2), cat)


def test_happy_path_reaches_completed(job):
    transition(job, Event.START, Actor.FUNDI, NOW)
    assert job.status == JobStatus.IN_PROGRESS and job.started_at == NOW
    transition(job, Event.MARK_COMPLETE, Actor.FUNDI, NOW)
    assert job.status == JobStatus.AWAITING_CONFIRMATION and job.marked_complete_at == NOW
    transition(job, Event.CONFIRM, Actor.CUSTOMER, NOW)
    assert job.status == JobStatus.COMPLETED and job.completed_at == NOW


def test_cannot_skip_to_complete_from_booked(job):
    with pytest.raises(InvalidTransition):
        transition(job, Event.MARK_COMPLETE, Actor.FUNDI, NOW)


def test_cannot_confirm_before_fundi_marks_complete(job):
    with pytest.raises(InvalidTransition):
        transition(job, Event.CONFIRM, Actor.CUSTOMER, NOW)
    transition(job, Event.START, Actor.FUNDI, NOW)
    with pytest.raises(InvalidTransition):
        transition(job, Event.CONFIRM, Actor.CUSTOMER, NOW)


def test_customer_cannot_start_or_mark_complete(job):
    with pytest.raises(NotPermitted):
        transition(job, Event.START, Actor.CUSTOMER, NOW)
    transition(job, Event.START, Actor.FUNDI, NOW)
    with pytest.raises(NotPermitted):
        transition(job, Event.MARK_COMPLETE, Actor.CUSTOMER, NOW)


def test_admin_cannot_confirm(job):
    transition(job, Event.START, Actor.FUNDI, NOW)
    transition(job, Event.MARK_COMPLETE, Actor.FUNDI, NOW)
    with pytest.raises(NotPermitted):
        transition(job, Event.CONFIRM, Actor.ADMIN, NOW)


@pytest.mark.parametrize("actor", [Actor.CUSTOMER, Actor.FUNDI, Actor.ADMIN])
def test_booked_job_can_be_cancelled_by_anyone_involved(job, actor):
    transition(job, Event.CANCEL, actor, NOW)
    assert job.status == JobStatus.CANCELLED and job.cancelled_by == actor.value


def test_awaiting_confirmation_can_only_be_cancelled_by_admin(job):
    transition(job, Event.START, Actor.FUNDI, NOW)
    transition(job, Event.MARK_COMPLETE, Actor.FUNDI, NOW)
    with pytest.raises(NotPermitted):
        transition(job, Event.CANCEL, Actor.FUNDI, NOW)
    transition(job, Event.CANCEL, Actor.ADMIN, NOW)


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.NO_SHOW])
@pytest.mark.parametrize("event", list(Event))
def test_terminal_states_accept_nothing(job, status, event):
    job.status = status
    with pytest.raises(InvalidTransition):
        transition(job, event, Actor.ADMIN, NOW)


def test_no_show_needs_30_minute_grace(job):
    job.scheduled_for = NOW - timedelta(minutes=29)
    with pytest.raises(TooEarly):
        transition(job, Event.REPORT_NO_SHOW, Actor.CUSTOMER, NOW)
    job.scheduled_for = NOW - timedelta(minutes=31)
    transition(job, Event.REPORT_NO_SHOW, Actor.CUSTOMER, NOW)
    assert job.status == JobStatus.NO_SHOW


def test_only_the_customer_reports_no_show(job):
    job.scheduled_for = NOW - timedelta(hours=2)
    with pytest.raises(NotPermitted):
        transition(job, Event.REPORT_NO_SHOW, Actor.FUNDI, NOW)


def test_no_show_not_possible_once_started(job):
    transition(job, Event.START, Actor.FUNDI, NOW)
    job.scheduled_for = NOW - timedelta(hours=2)
    with pytest.raises(InvalidTransition):
        transition(job, Event.REPORT_NO_SHOW, Actor.CUSTOMER, NOW)


def test_allowed_events_by_actor():
    assert allowed_events(JobStatus.BOOKED, Actor.FUNDI) == [Event.START, Event.CANCEL]
    assert Event.CONFIRM in allowed_events(JobStatus.AWAITING_CONFIRMATION, Actor.CUSTOMER)
    assert allowed_events(JobStatus.COMPLETED, Actor.ADMIN) == []


def test_only_confirm_reaches_completed():
    reaching = [(k, v) for k, v in TRANSITIONS.items() if v[0] == JobStatus.COMPLETED]
    assert [k for k, _ in reaching] == [(JobStatus.AWAITING_CONFIRMATION, Event.CONFIRM)]


def test_on_time_rules(job):
    assert was_on_time(job) is None
    job.started_at = job.scheduled_for + timedelta(minutes=15)
    assert was_on_time(job) is True
    job.started_at = job.scheduled_for + timedelta(minutes=16)
    assert was_on_time(job) is False


def test_cancellation_blame(job):
    job.status, job.cancelled_by = JobStatus.CANCELLED, "customer"
    assert was_on_time(job) is None
    job.cancelled_by = "fundi"
    assert was_on_time(job) is False
    job.status = JobStatus.NO_SHOW
    assert was_on_time(job) is False
