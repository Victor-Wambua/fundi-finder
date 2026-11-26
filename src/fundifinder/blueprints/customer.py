"""Customer flow: request a quote, accept, book, confirm on site, review, report."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import clock, marketplace, moderation, queries, reviews
from ..completion import CodeResult, derive_code
from ..forms import Form
from ..lifecycle import NO_SHOW_GRACE, Event
from ..marketplace import RuleViolation
from ..models import (
    AppealStatus,
    FundiProfile,
    Job,
    JobStatus,
    Quote,
    QuoteRequest,
    Report,
    ReportStatus,
    RequestStatus,
    Review,
    Role,
)
from ..security import login_required, rate_limit, role_required
from ..uploads import mime_for
from ..web import db, get_or_404, require_user, rule_error, save_upload_or_flash, secret, storage

bp = Blueprint("customer", __name__)

STEPS = [
    ("booked", "Booked"),
    ("in_progress", "Fundi on site"),
    ("awaiting_confirmation", "Work done"),
    ("completed", "Confirmed with code"),
]


@bp.route("/requests/new", methods=["GET", "POST"])
@role_required(Role.CUSTOMER)
def new_request() -> ResponseReturnValue:
    session = db()
    user = require_user()
    profile = session.get(FundiProfile, request.values.get("fundi", type=int) or 0)
    if profile is None or profile.user.is_suspended:
        abort(404)
    benchmarks = queries.benchmarks_by_job_type(session)
    errors: dict[str, str] = {}
    form: dict[str, Any] = {}
    default_time = (clock.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    if request.method == "POST":
        rate_limit("request-new", str(user.id), 10, 3600)
        f = Form(request.form)
        service_ids = {str(s.job_type_id) for s in profile.services}
        job_type_id = int(f.choice("job_type", "job", service_ids) or 0)
        area_id = int(f.choice("area", "area", {str(a.id) for a in profile.areas}) or 0)
        description = f.text("description", "Description", 15, 1500)
        when = f.datetime_local("preferred_time", "your preferred time")
        photo_name: str | None = None
        photo_ok = True
        if f.ok:
            photo_name, photo_ok = save_upload_or_flash("photo", "private")
        if f.ok and photo_ok and when is not None:
            try:
                req = marketplace.create_request(
                    session, user, profile, job_type_id, area_id, description, when, photo_name
                )
                session.commit()
                flash(
                    f"Request sent to {profile.user.first_name}. You will see their quote here.",
                    "ok",
                )
                return redirect(url_for("customer.request_detail", request_id=req.id))
            except RuleViolation as exc:
                session.rollback()
                if photo_name:
                    storage().delete("private", photo_name)
                rule_error(exc)
        errors = f.errors
        form = dict(request.form)
    return render_template(
        "customer/new_request.html",
        p=profile,
        benchmarks=benchmarks,
        form=form,
        errors=errors,
        default_time=default_time,
        needs_phone=user.phone_verified_at is None,
        area_options=[(a.id, a.name) for a in profile.areas],
    )


@bp.get("/me")
@role_required(Role.CUSTOMER)
def activity() -> ResponseReturnValue:
    session = db()
    user = require_user()
    jobs = list(
        session.scalars(
            select(Job)
            .where(Job.customer_id == user.id)
            .options(
                selectinload(Job.fundi).selectinload(FundiProfile.user), selectinload(Job.review)
            )
            .order_by(Job.scheduled_for.desc())
        )
    )
    requests_ = list(
        session.scalars(
            select(QuoteRequest)
            .where(
                QuoteRequest.customer_id == user.id, QuoteRequest.status != RequestStatus.ACCEPTED
            )
            .options(
                selectinload(QuoteRequest.fundi).selectinload(FundiProfile.user),
                selectinload(QuoteRequest.quotes),
            )
            .order_by(QuoteRequest.created_at.desc())
        )
    )
    return render_template("customer/activity.html", jobs=jobs, requests=requests_)


@bp.get("/me/requests/<int:request_id>")
@role_required(Role.CUSTOMER)
def request_detail(request_id: int) -> ResponseReturnValue:
    req = get_or_404(db(), QuoteRequest, request_id)
    if req.customer_id != require_user().id:
        abort(404)
    job = db().scalar(select(Job).where(Job.request_id == req.id))
    bench = queries.job_type_benchmark(db(), req.job_type_id)
    return render_template("customer/request_detail.html", req=req, job=job, bench=bench)


@bp.post("/me/requests/<int:request_id>/cancel")
@role_required(Role.CUSTOMER)
def cancel_request(request_id: int) -> ResponseReturnValue:
    req = get_or_404(db(), QuoteRequest, request_id)
    if req.customer_id != require_user().id:
        abort(404)
    try:
        marketplace.cancel_request(db(), require_user(), req)
        db().commit()
        flash("Request cancelled.", "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("customer.activity"))


@bp.post("/me/quotes/<int:quote_id>/accept")
@role_required(Role.CUSTOMER)
def accept_quote(quote_id: int) -> ResponseReturnValue:
    quote = get_or_404(db(), Quote, quote_id)
    if quote.request.customer_id != require_user().id:
        abort(404)
    try:
        job = marketplace.accept_quote(db(), require_user(), quote)
        db().commit()
        flash("Booked. Your completion code is on this page. Keep it for the day.", "ok")
        return redirect(url_for("customer.job_detail", job_id=job.id))
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
        return redirect(url_for("customer.request_detail", request_id=quote.request_id))


def _own_job(job_id: int) -> Job:
    job = get_or_404(db(), Job, job_id)
    if job.customer_id != require_user().id:
        abort(404)
    return job


@bp.get("/me/jobs/<int:job_id>")
@role_required(Role.CUSTOMER)
def job_detail(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    status = JobStatus(job.status)
    show_code = status in (JobStatus.BOOKED, JobStatus.IN_PROGRESS, JobStatus.AWAITING_CONFIRMATION)
    order = [s for s, _ in STEPS]
    current = order.index(status.value) if status.value in order else -1
    block = reviews.review_block_reason(db(), require_user(), job)
    can_edit_review = (
        job.review is not None
        and job.review.status == "published"
        and clock.now() - job.review.created_at <= reviews.EDIT_WINDOW
    )
    return render_template(
        "customer/job_detail.html",
        job=job,
        steps=STEPS,
        current=current,
        code=derive_code(secret(), job.code_salt)
        if show_code and not job.code_invalidated
        else None,
        can_review=block is None,
        review_block=block,
        can_edit_review=can_edit_review,
        can_report_no_show=status is JobStatus.BOOKED
        and clock.now() >= job.scheduled_for + NO_SHOW_GRACE,
        JobStatus=JobStatus,
    )


@bp.post("/me/jobs/<int:job_id>/confirm")
@role_required(Role.CUSTOMER)
def confirm(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    rate_limit("code", str(job.id), 30, 3600)
    try:
        result = marketplace.confirm_completion(
            db(), require_user(), job, request.form.get("code", ""), secret()
        )
    except RuleViolation as exc:
        rule_error(exc)
        return redirect(url_for("customer.job_detail", job_id=job.id))
    _flash_code_result(result.result, result.attempts_left)
    return redirect(url_for("customer.job_detail", job_id=job.id))


def _flash_code_result(result: CodeResult, attempts_left: int) -> None:
    messages = {
        CodeResult.OK: ("Confirmed. The job is complete, and you can now leave a review.", "ok"),
        CodeResult.WRONG: (
            f"That code is not right. {attempts_left} attempt(s) left before a lockout.",
            "error",
        ),
        CodeResult.LOCKED: ("Too many wrong codes. Entry is locked for an hour.", "error"),
        CodeResult.INVALIDATED: (
            "This code has been disabled after too many wrong attempts. Support must re-issue it.",
            "error",
        ),
        CodeResult.USED: ("This code has already been used.", "error"),
    }
    text, cat = messages[result]
    flash(text, cat)


@bp.post("/me/jobs/<int:job_id>/cancel")
@role_required(Role.CUSTOMER)
def cancel_job(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    try:
        marketplace.apply_event(
            db(), require_user(), job, Event.CANCEL, request.form.get("reason", "")
        )
        db().commit()
        flash("Job cancelled.", "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("customer.job_detail", job_id=job.id))


@bp.post("/me/jobs/<int:job_id>/no-show")
@role_required(Role.CUSTOMER)
def no_show(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    try:
        marketplace.apply_event(db(), require_user(), job, Event.REPORT_NO_SHOW)
        db().commit()
        flash("Recorded as a no-show. We are sorry about that.", "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("customer.job_detail", job_id=job.id))


@bp.post("/me/jobs/<int:job_id>/review")
@role_required(Role.CUSTOMER)
def create_review(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    rate_limit("review", str(require_user().id), 10, 3600)
    f = Form(request.form)
    rating = f.integer("rating", "Rating", 1, 5)
    body = f.text("body", "Review", 0, reviews.MAX_REVIEW_CHARS, required=False)
    if not f.ok or rating is None:
        flash("Choose a rating from 1 to 5.", "error")
        return redirect(url_for("customer.job_detail", job_id=job.id))
    try:
        reviews.create_review(db(), require_user(), job, rating, body)
        db().commit()
        flash("Thank you. Your review is live on the fundi's profile.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("customer.job_detail", job_id=job.id))


@bp.post("/me/reviews/<int:review_id>/edit")
@role_required(Role.CUSTOMER)
def edit_review(review_id: int) -> ResponseReturnValue:
    review = get_or_404(db(), Review, review_id)
    if review.customer_id != require_user().id:
        abort(404)
    f = Form(request.form)
    rating = f.integer("rating", "Rating", 1, 5)
    body = f.text("body", "Review", 0, reviews.MAX_REVIEW_CHARS, required=False)
    if not f.ok or rating is None:
        flash("Choose a rating from 1 to 5.", "error")
    else:
        try:
            reviews.edit_review(db(), require_user(), review, rating, body)
            db().commit()
            flash("Review updated.", "ok")
        except RuleViolation as exc:
            rule_error(exc)
    return redirect(url_for("customer.job_detail", job_id=review.job_id))


@bp.get("/uploads/request/<int:request_id>")
@login_required
def request_photo(request_id: int) -> ResponseReturnValue:
    """Quote-request photos are private: customer, the requested fundi, or an admin."""
    req = get_or_404(db(), QuoteRequest, request_id)
    user = require_user()
    allowed = (
        user.id == req.customer_id
        or user.role == Role.ADMIN
        or (user.fundi_profile is not None and user.fundi_profile.id == req.fundi_id)
    )
    if not allowed or not req.photo_name:
        abort(404)
    path = storage().path("private", req.photo_name)
    if path is None:
        abort(404)
    resp = send_file(path, mimetype=mime_for(req.photo_name))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.post("/reviews/<int:review_id>/report")
@login_required
def report_review(review_id: int) -> ResponseReturnValue:
    review = get_or_404(db(), Review, review_id)
    user = require_user()
    rate_limit("report", str(user.id), 10, 3600)
    try:
        moderation.create_report(
            db(),
            user,
            review,
            request.form.get("target", "review"),
            request.form.get("reason", ""),
            request.form.get("detail", ""),
        )
        db().commit()
        flash("Thanks. A moderator will look at this.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("public.profile", fundi_id=review.fundi_id) + f"#review-{review.id}")


@bp.get("/me/notices")
@login_required
def notices() -> ResponseReturnValue:
    """Takedowns of the user's own content, with the appeal form."""
    user = require_user()
    reports = [
        r
        for r in db().scalars(
            select(Report)
            .where(Report.status == ReportStatus.UPHELD)
            .options(selectinload(Report.review).selectinload(Review.fundi))
            .order_by(Report.resolved_at.desc())
        )
        if moderation.author_of(r).id == user.id
    ]
    return render_template("notices.html", reports=reports, AppealStatus=AppealStatus)


@bp.post("/me/reports/<int:report_id>/appeal")
@login_required
def appeal(report_id: int) -> ResponseReturnValue:
    report = get_or_404(db(), Report, report_id)
    try:
        moderation.submit_appeal(db(), require_user(), report, request.form.get("text", ""))
        db().commit()
        flash("Appeal submitted. A different admin will review it.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("customer.notices"))
