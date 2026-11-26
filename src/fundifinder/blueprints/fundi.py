"""Fundi flow: profile, availability, requests inbox, quotes, jobs, earnings, verification."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import clock, marketplace, queries, reviews, verification
from ..availability import WEEKDAYS
from ..completion import CodeResult
from ..forms import Form
from ..lifecycle import Event
from ..marketplace import RuleViolation
from ..models import (
    Area,
    AvailabilityException,
    AvailabilityRule,
    FundiProfile,
    FundiService,
    IdDocument,
    Job,
    JobStatus,
    JobType,
    PortfolioPhoto,
    QuoteRequest,
    Reference,
    RequestStatus,
    Review,
    Role,
    Trade,
)
from ..security import rate_limit, role_required
from ..uploads import UploadError
from ..web import (
    db,
    get_or_404,
    read_upload,
    require_user,
    rule_error,
    save_upload_or_flash,
    secret,
    storage,
)

bp = Blueprint("fundi", __name__, url_prefix="/fundi")


def me() -> FundiProfile:
    profile = require_user().fundi_profile
    if profile is None:
        abort(403)
    return profile


@bp.get("/")
@role_required(Role.FUNDI)
def dashboard() -> ResponseReturnValue:
    session = db()
    profile = me()
    now = clock.now()
    card = queries.load_cards(session, [profile], now)[0]
    jobs = list(session.scalars(select(Job).where(Job.fundi_id == profile.id)))
    completed = [j for j in jobs if j.status == JobStatus.COMPLETED and j.completed_at]
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    earnings = {
        "month": sum(j.price_kes for j in completed if j.completed_at and j.completed_at >= month_start),
        "d90": sum(j.price_kes for j in completed if j.completed_at and j.completed_at >= now - timedelta(days=90)),
        "all": sum(j.price_kes for j in completed),
        "pending": sum(j.price_kes for j in jobs if j.status == JobStatus.AWAITING_CONFIRMATION),
        "booked": sum(j.price_kes for j in jobs if j.status in (JobStatus.BOOKED, JobStatus.IN_PROGRESS)),
    }
    open_requests = [
        r for r in marketplace.list_requests_for_fundi(session, profile.id)
        if r.status == RequestStatus.OPEN
    ]
    upcoming = sorted(
        (j for j in jobs if j.status in (JobStatus.BOOKED, JobStatus.IN_PROGRESS)),
        key=lambda j: j.scheduled_for,
    )[:5]
    listed = bool(profile.services and profile.areas)
    return render_template(
        "fundi/dashboard.html",
        card=card,
        earnings=earnings,
        open_requests=open_requests,
        upcoming=upcoming,
        listed=listed,
        awaiting=[j for j in jobs if j.status == JobStatus.AWAITING_CONFIRMATION],
    )


# ---- profile and services --------------------------------------------------------------


@bp.route("/profile", methods=["GET", "POST"])
@role_required(Role.FUNDI)
def profile_edit() -> ResponseReturnValue:
    session = db()
    profile = me()
    areas = list(session.scalars(select(Area).order_by(Area.name)))
    trades = list(session.scalars(select(Trade).order_by(Trade.name)))
    errors: dict[str, str] = {}
    if request.method == "POST":
        f = Form(request.form)
        headline = f.text("headline", "Headline", 5, 140)
        bio = f.text("bio", "About you", 0, 1500, required=False)
        years = f.integer("years_experience", "Years of experience", 0, 60)
        trade_slug = f.choice("trade", "trade", {t.slug for t in trades})
        whatsapp_raw = f.text("whatsapp", "WhatsApp number", 0, 20, required=False)
        whatsapp = None
        if whatsapp_raw:
            from ..phones import normalise_phone

            whatsapp = normalise_phone(whatsapp_raw)
            if whatsapp is None:
                f.errors["whatsapp"] = "Enter a Kenyan mobile number, like 0700 000 001."
        chosen = {int(a) for a in request.form.getlist("areas") if a.isdigit()}
        valid_ids = {a.id for a in areas}
        if not chosen or not chosen <= valid_ids:
            f.errors["areas"] = "Choose at least one area you work in."
        if f.ok and years is not None:
            profile.headline = headline
            profile.bio = bio
            profile.years_experience = years
            profile.whatsapp = whatsapp
            profile.trade_id = next(t.id for t in trades if t.slug == trade_slug)
            profile.areas = [a for a in areas if a.id in chosen]
            session.commit()
            flash("Profile saved.", "ok")
            return redirect(url_for("fundi.profile_edit"))
        errors = f.errors
    job_types = list(
        session.scalars(
            select(JobType).where(JobType.trade_id == profile.trade_id).order_by(JobType.name)
        )
    )
    benchmarks = queries.benchmarks_by_job_type(session)
    return render_template(
        "fundi/profile.html",
        p=profile,
        areas=areas,
        trades=trades,
        trade_options=[(t.slug, t.name) for t in trades],
        job_type_options=[(j.id, j.name) for j in job_types],
        chosen={a.id for a in profile.areas},
        benchmarks=benchmarks,
        errors=errors,
        form=dict(request.form) if request.method == "POST" else {},
    )


@bp.post("/services")
@role_required(Role.FUNDI)
def service_save() -> ResponseReturnValue:
    session = db()
    profile = me()
    f = Form(request.form)
    valid = {str(j.id) for j in session.scalars(select(JobType).where(JobType.trade_id == profile.trade_id))}
    jt = int(f.choice("job_type", "job", valid) or 0)
    low = f.integer("price_min", "Lowest price", 100, 5_000_000)
    high = f.integer("price_max", "Highest price", 100, 5_000_000)
    if f.ok and low is not None and high is not None and high < low:
        f.errors["price_max"] = "The highest price must be at least the lowest price."
    if not f.ok or low is None or high is None:
        flash(" ".join(f.errors.values()), "error")
        return redirect(url_for("fundi.profile_edit") + "#services")
    existing = next((s for s in profile.services if s.job_type_id == jt), None)
    if existing:
        existing.price_min_kes, existing.price_max_kes = low, high
    else:
        profile.services.append(FundiService(job_type_id=jt, price_min_kes=low, price_max_kes=high))
    session.commit()
    flash("Service saved.", "ok")
    return redirect(url_for("fundi.profile_edit") + "#services")


@bp.post("/services/<int:service_id>/delete")
@role_required(Role.FUNDI)
def service_delete(service_id: int) -> ResponseReturnValue:
    service = get_or_404(db(), FundiService, service_id)
    if service.fundi_id != me().id:
        abort(404)
    db().delete(service)
    db().commit()
    flash("Service removed.", "ok")
    return redirect(url_for("fundi.profile_edit") + "#services")


# ---- availability ----------------------------------------------------------------------


@bp.route("/availability", methods=["GET", "POST"])
@role_required(Role.FUNDI)
def availability() -> ResponseReturnValue:
    session = db()
    profile = me()
    if request.method == "POST":
        rules: list[AvailabilityRule] = []
        for day in range(7):
            if request.form.get(f"day{day}") != "on":
                continue
            try:
                start = int(request.form.get(f"start{day}", "8"))
                end = int(request.form.get(f"end{day}", "17"))
            except ValueError:
                flash("Hours must be whole numbers.", "error")
                return redirect(url_for("fundi.availability"))
            if not (0 <= start < end <= 24):
                flash(f"{WEEKDAYS[day]}: the finish time must be after the start time.", "error")
                return redirect(url_for("fundi.availability"))
            rules.append(AvailabilityRule(fundi_id=profile.id, weekday=day, start_hour=start, end_hour=end))
        for old in session.scalars(select(AvailabilityRule).where(AvailabilityRule.fundi_id == profile.id)):
            session.delete(old)
        session.flush()
        session.add_all(rules)
        session.commit()
        flash("Weekly hours saved.", "ok")
        return redirect(url_for("fundi.availability"))
    rules_by_day = {
        r.weekday: r
        for r in session.scalars(select(AvailabilityRule).where(AvailabilityRule.fundi_id == profile.id))
    }
    exceptions = list(
        session.scalars(
            select(AvailabilityException)
            .where(AvailabilityException.fundi_id == profile.id, AvailabilityException.day >= clock.now().date())
            .order_by(AvailabilityException.day)
        )
    )
    return render_template(
        "fundi/availability.html", rules=rules_by_day, exceptions=exceptions, weekdays=WEEKDAYS
    )


@bp.post("/availability/exceptions")
@role_required(Role.FUNDI)
def exception_add() -> ResponseReturnValue:
    session = db()
    profile = me()
    f = Form(request.form)
    day = f.date_value("day", "the exception")
    available = request.form.get("available") == "1"
    note = f.text("note", "Note", 0, 120, required=False)
    if not f.ok or day is None:
        flash(" ".join(f.errors.values()), "error")
    elif day < clock.now().date():
        flash("Choose a date in the future.", "error")
    else:
        existing = session.scalar(
            select(AvailabilityException).where(
                AvailabilityException.fundi_id == profile.id, AvailabilityException.day == day
            )
        )
        if existing:
            existing.available, existing.note = available, note
        else:
            session.add(AvailabilityException(fundi_id=profile.id, day=day, available=available, note=note))
        session.commit()
        flash("Date saved.", "ok")
    return redirect(url_for("fundi.availability"))


@bp.post("/availability/exceptions/<int:exception_id>/delete")
@role_required(Role.FUNDI)
def exception_delete(exception_id: int) -> ResponseReturnValue:
    exc = get_or_404(db(), AvailabilityException, exception_id)
    if exc.fundi_id != me().id:
        abort(404)
    db().delete(exc)
    db().commit()
    return redirect(url_for("fundi.availability"))


# ---- portfolio -------------------------------------------------------------------------


@bp.route("/portfolio", methods=["GET", "POST"])
@role_required(Role.FUNDI)
def portfolio() -> ResponseReturnValue:
    session = db()
    profile = me()
    if request.method == "POST":
        rate_limit("portfolio", str(profile.id), 20, 3600)
        caption = (request.form.get("caption") or "").strip()[:140]
        if read_upload("photo") is None:
            flash("Choose a photo to upload.", "error")
        elif len(profile.photos) >= 12:
            flash("You can keep up to 12 portfolio photos.", "error")
        else:
            name, ok = save_upload_or_flash("photo", "public")
            if ok and name:
                profile.photos.append(PortfolioPhoto(storage_name=name, caption=caption))
                session.commit()
                flash("Photo added.", "ok")
        return redirect(url_for("fundi.portfolio"))
    return render_template("fundi/portfolio.html", p=profile)


@bp.post("/portfolio/<int:photo_id>/delete")
@role_required(Role.FUNDI)
def portfolio_delete(photo_id: int) -> ResponseReturnValue:
    photo = get_or_404(db(), PortfolioPhoto, photo_id)
    if photo.fundi_id != me().id:
        abort(404)
    storage().delete("public", photo.storage_name)
    db().delete(photo)
    db().commit()
    flash("Photo removed.", "ok")
    return redirect(url_for("fundi.portfolio"))


# ---- verification ----------------------------------------------------------------------


@bp.get("/verification")
@role_required(Role.FUNDI)
def verification_page() -> ResponseReturnValue:
    session = db()
    profile = me()
    card = queries.load_cards(session, [profile])[0]
    docs = list(
        session.scalars(select(IdDocument).where(IdDocument.fundi_id == profile.id).order_by(IdDocument.id.desc()))
    )
    refs = list(session.scalars(select(Reference).where(Reference.fundi_id == profile.id)))
    return render_template("fundi/verification.html", card=card, docs=docs, refs=refs, p=profile)


@bp.post("/verification/id")
@role_required(Role.FUNDI)
def id_upload() -> ResponseReturnValue:
    profile = me()
    rate_limit("id-upload", str(profile.id), 5, 3600)
    got = read_upload("document")
    if got is None:
        flash("Choose a photo of your ID document.", "error")
        return redirect(url_for("fundi.verification_page"))
    data, filename = got
    try:
        verification.submit_id_document(
            db(), profile, data, filename, storage(), int(current_app.config["MAX_UPLOAD_BYTES"])
        )
        db().commit()
        flash("Document received. An admin will review it. It is stored privately and never shown on your profile.", "ok")
    except (UploadError, RuleViolation) as exc:
        db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("fundi.verification_page"))


@bp.post("/verification/references")
@role_required(Role.FUNDI)
def reference_add() -> ResponseReturnValue:
    profile = me()
    rate_limit("reference", str(profile.id), 10, 3600)
    f = Form(request.form)
    name = f.text("name", "Name", 2, 80)
    note = f.text("note", "How they know you", 0, 120, required=False)
    if not f.ok:
        flash(" ".join(f.errors.values()), "error")
        return redirect(url_for("fundi.verification_page"))
    try:
        verification.add_reference(
            db(), profile, name, request.form.get("phone", ""), note,
            current_app.extensions["sms"], request.host_url.rstrip("/"),
        )
        db().commit()
        flash("Reference added. They have been sent an SMS asking them to confirm.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("fundi.verification_page"))


# ---- requests and quotes ---------------------------------------------------------------


@bp.get("/requests")
@role_required(Role.FUNDI)
def requests_inbox() -> ResponseReturnValue:
    items = marketplace.list_requests_for_fundi(db(), me().id)
    return render_template("fundi/requests.html", items=items)


def _own_request(request_id: int) -> QuoteRequest:
    req = get_or_404(db(), QuoteRequest, request_id)
    if req.fundi_id != me().id:
        abort(404)
    return req


@bp.get("/requests/<int:request_id>")
@role_required(Role.FUNDI)
def request_detail(request_id: int) -> ResponseReturnValue:
    req = _own_request(request_id)
    service = next((s for s in me().services if s.job_type_id == req.job_type_id), None)
    bench = queries.job_type_benchmark(db(), req.job_type_id)
    default_start = max(req.preferred_time, clock.now() + timedelta(hours=2))
    return render_template(
        "fundi/request_detail.html", req=req, service=service, bench=bench,
        default_start=default_start, errors={}, form={},
    )


@bp.post("/requests/<int:request_id>/quote")
@role_required(Role.FUNDI)
def send_quote(request_id: int) -> ResponseReturnValue:
    req = _own_request(request_id)
    rate_limit("quote", str(me().id), 40, 3600)
    f = Form(request.form)
    amount = f.integer("amount", "Price", marketplace.MIN_QUOTE_KES, marketplace.MAX_QUOTE_KES)
    message = f.text("message", "Message", 0, 800, required=False)
    start = f.datetime_local("start", "the start time")
    if f.ok and amount is not None and start is not None:
        try:
            marketplace.send_quote(db(), require_user(), req, amount, message, start)
            db().commit()
            flash("Quote sent. The customer will be notified.", "ok")
            return redirect(url_for("fundi.requests_inbox"))
        except RuleViolation as exc:
            db().rollback()
            f.errors["amount"] = str(exc)
    service = next((s for s in me().services if s.job_type_id == req.job_type_id), None)
    return render_template(
        "fundi/request_detail.html", req=req, service=service,
        bench=queries.job_type_benchmark(db(), req.job_type_id),
        default_start=clock.now() + timedelta(hours=2), errors=f.errors, form=dict(request.form),
    ), 400


@bp.post("/requests/<int:request_id>/decline")
@role_required(Role.FUNDI)
def decline(request_id: int) -> ResponseReturnValue:
    req = _own_request(request_id)
    try:
        marketplace.decline_request(db(), require_user(), req)
        db().commit()
        flash("Request declined.", "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("fundi.requests_inbox"))


# ---- jobs ------------------------------------------------------------------------------


@bp.get("/jobs")
@role_required(Role.FUNDI)
def jobs() -> ResponseReturnValue:
    items = list(
        db().scalars(
            select(Job)
            .where(Job.fundi_id == me().id)
            .options(selectinload(Job.customer), selectinload(Job.job_type), selectinload(Job.area))
            .order_by(Job.scheduled_for.desc())
        )
    )
    return render_template("fundi/jobs.html", items=items)


def _own_job(job_id: int) -> Job:
    job = get_or_404(db(), Job, job_id)
    if job.fundi_id != me().id:
        abort(404)
    return job


@bp.get("/jobs/<int:job_id>")
@role_required(Role.FUNDI)
def job_detail(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    return render_template("fundi/job_detail.html", job=job, JobStatus=JobStatus)


def _event(job_id: int, event: Event, ok_message: str) -> ResponseReturnValue:
    job = _own_job(job_id)
    try:
        marketplace.apply_event(db(), require_user(), job, event, request.form.get("reason", ""))
        db().commit()
        flash(ok_message, "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("fundi.job_detail", job_id=job.id))


@bp.post("/jobs/<int:job_id>/start")
@role_required(Role.FUNDI)
def start(job_id: int) -> ResponseReturnValue:
    return _event(job_id, Event.START, "Marked as started. Your arrival time is recorded.")


@bp.post("/jobs/<int:job_id>/complete")
@role_required(Role.FUNDI)
def mark_complete(job_id: int) -> ResponseReturnValue:
    return _event(
        job_id, Event.MARK_COMPLETE,
        "Marked complete. Now ask the customer for their completion code.",
    )


@bp.post("/jobs/<int:job_id>/cancel")
@role_required(Role.FUNDI)
def cancel(job_id: int) -> ResponseReturnValue:
    return _event(job_id, Event.CANCEL, "Job cancelled.")


@bp.post("/jobs/<int:job_id>/code")
@role_required(Role.FUNDI)
def enter_code(job_id: int) -> ResponseReturnValue:
    job = _own_job(job_id)
    rate_limit("code", str(job.id), 30, 3600)
    try:
        result = marketplace.confirm_completion(
            db(), require_user(), job, request.form.get("code", ""), secret()
        )
    except RuleViolation as exc:
        rule_error(exc)
        return redirect(url_for("fundi.job_detail", job_id=job.id))
    if result.result is CodeResult.OK:
        flash("Confirmed. The job is complete and the customer can now review it.", "ok")
    elif result.result is CodeResult.WRONG:
        flash(f"That code is not right. {result.attempts_left} attempt(s) left before a lockout.", "error")
    elif result.result is CodeResult.LOCKED:
        flash("Too many wrong codes. Entry is locked for an hour.", "error")
    elif result.result is CodeResult.INVALIDATED:
        flash("This code was disabled after too many wrong attempts. Contact support.", "error")
    else:
        flash("This code has already been used.", "error")
    return redirect(url_for("fundi.job_detail", job_id=job.id))


# ---- reviews ---------------------------------------------------------------------------


@bp.get("/reviews")
@role_required(Role.FUNDI)
def reviews_page() -> ResponseReturnValue:
    items = list(
        db().scalars(
            select(Review)
            .where(Review.fundi_id == me().id)
            .options(selectinload(Review.customer), selectinload(Review.job))
            .order_by(Review.created_at.desc())
        )
    )
    return render_template("fundi/reviews.html", items=items)


@bp.post("/reviews/<int:review_id>/reply")
@role_required(Role.FUNDI)
def reply(review_id: int) -> ResponseReturnValue:
    review = get_or_404(db(), Review, review_id)
    if review.fundi_id != me().id:
        abort(404)
    try:
        reviews.reply_to_review(db(), require_user(), review, request.form.get("body", ""))
        db().commit()
        flash("Reply published.", "ok")
    except RuleViolation as exc:
        rule_error(exc)
    return redirect(url_for("fundi.reviews_page"))

