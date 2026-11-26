"""Public pages: home, search, fundi profile, trust explainer, reference replies."""

from __future__ import annotations

from urllib.parse import urlencode

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .. import clock, queries
from ..availability import WEEKDAYS, WeeklyRule, summarise
from ..marketplace import RuleViolation
from ..models import (
    Area,
    AvailabilityRule,
    FundiProfile,
    Job,
    JobStatus,
    Reference,
    RefStatus,
    Review,
    ReviewStatus,
    Trade,
)
from ..phones import display_phone, tel_url, whatsapp_url
from ..ranking import wilson_lower_bound
from ..security import client_key, current_user, rate_limit
from ..sms import ConsoleSms
from ..uploads import mime_for
from ..verification import respond_to_reference
from ..web import db, is_htmx, storage

bp = Blueprint("public", __name__)
PAGE_SIZE = 12


def _int_arg(name: str) -> int | None:
    raw = (request.args.get(name) or "").replace(",", "").strip()
    return int(raw) if raw.isdigit() else None


@bp.get("/")
def home() -> ResponseReturnValue:
    session = db()
    trades = list(session.scalars(select(Trade).order_by(Trade.name)))
    areas = list(session.scalars(select(Area).order_by(Area.name)))
    cards = queries.search_fundis(session, queries.SearchFilters())[:3]
    totals = {
        "fundis": session.scalar(select(func.count()).select_from(FundiProfile)) or 0,
        "jobs": session.scalar(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.COMPLETED)
        )
        or 0,
        "reviews": session.scalar(
            select(func.count()).select_from(Review).where(Review.status == ReviewStatus.PUBLISHED)
        )
        or 0,
    }
    return render_template("home.html", trades=trades, areas=areas, cards=cards, totals=totals)


@bp.get("/search")
def search() -> ResponseReturnValue:
    session = db()
    trades = list(session.scalars(select(Trade).order_by(Trade.name)))
    areas = list(session.scalars(select(Area).order_by(Area.name)))
    trade = next((t for t in trades if t.slug == request.args.get("trade")), None)
    area = next((a for a in areas if a.slug == request.args.get("area")), None)
    filters = queries.SearchFilters(
        trade_id=trade.id if trade else None,
        area_id=area.id if area else None,
        verified_only=request.args.get("verified") == "1",
        available_week=request.args.get("available") == "1",
        min_price=_int_arg("min_price"),
        max_price=_int_arg("max_price"),
    )
    all_cards = queries.search_fundis(session, filters)
    page = max(1, request.args.get("page", type=int) or 1)
    cards = all_cards[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    next_args = {k: v for k, v in request.args.items() if k != "page"}
    ctx = {
        "cards": cards,
        "total": len(all_cards),
        "has_more": page * PAGE_SIZE < len(all_cards),
        "next_url": url_for("public.search") + "?" + urlencode({**next_args, "page": page + 1}),
        "trade": trade,
        "area": area,
        "filters": filters,
        "args": request.args,
        "trades": trades,
        "areas": areas,
    }
    if is_htmx():
        return render_template("_cards.html" if page > 1 else "_results.html", **ctx)
    return render_template("search.html", **ctx)


@bp.get("/fundis/<int:fundi_id>")
def profile(fundi_id: int) -> ResponseReturnValue:
    session = db()
    p = session.scalar(
        select(FundiProfile)
        .where(FundiProfile.id == fundi_id)
        .options(
            selectinload(FundiProfile.user),
            selectinload(FundiProfile.areas),
            selectinload(FundiProfile.services),
            selectinload(FundiProfile.photos),
        )
    )
    if p is None or p.user.is_suspended:
        abort(404)
    now = clock.now()
    card = queries.load_cards(session, [p], now)[0]
    reviews = list(
        session.scalars(
            select(Review)
            .where(Review.fundi_id == p.id, Review.status == ReviewStatus.PUBLISHED)
            .options(selectinload(Review.customer), selectinload(Review.job))
            .order_by(Review.created_at.desc())
            .limit(25)
        )
    )
    benchmarks = queries.benchmarks_by_job_type(session)
    rules = [
        WeeklyRule(r.weekday, r.start_hour, r.end_hour)
        for r in session.scalars(select(AvailabilityRule).where(AvailabilityRule.fundi_id == p.id))
    ]
    positive = sum(n for star, n in card.histogram.items() if star >= 4)
    wilson = wilson_lower_bound(positive, card.review_count)
    wa_text = f"Hi {p.user.first_name}, I found you on Fundi Finder."
    contact = p.whatsapp or p.user.phone
    all_badges = [
        ("phone", "Phone verified", "A one-time code was sent by SMS and entered."),
        (
            "id",
            "ID checked",
            "An admin reviewed an identity document. The document is never shown.",
        ),
        ("references", "References", "Two or more past customers confirmed they hired this fundi."),
        ("skills", "Trade skills", "Three or more photos of their own work."),
    ]
    earned = {b.key.value for b in card.badges}
    return render_template(
        "profile.html",
        card=card,
        p=p,
        reviews=reviews,
        benchmarks=benchmarks,
        availability=summarise(rules),
        weekdays=[(WEEKDAYS[i], any(r.weekday == i for r in rules)) for i in range(7)],
        wilson=wilson,
        positive=positive,
        recent=queries.recent_completed_jobs(session, p.id, now),
        wa_url=whatsapp_url(contact, wa_text),
        tel_url=tel_url(contact),
        contact_display=display_phone(contact),
        all_badges=[(k, label, why, k in earned) for k, label, why in all_badges],
        viewer=current_user(),
    )


@bp.get("/trust")
def trust() -> ResponseReturnValue:
    return render_template("trust.html")


@bp.get("/media/portfolio/<name>")
def portfolio_image(name: str) -> ResponseReturnValue:
    path = storage().path("public", name)
    if path is None:
        abort(404)
    return send_file(path, mimetype=mime_for(name), max_age=3600)


@bp.route("/ref/<token>", methods=["GET", "POST"])
def reference_reply(token: str) -> ResponseReturnValue:
    session = db()
    ref = session.scalar(
        select(Reference).where(Reference.token == token).options(selectinload(Reference.fundi))
    )
    if ref is None:
        abort(404)
    if request.method == "POST":
        rate_limit("ref", client_key(), 20, 3600)
        try:
            respond_to_reference(session, ref, request.form.get("answer") == "yes")
            session.commit()
            flash("Thank you. Your answer has been recorded.", "ok")
        except RuleViolation as exc:
            flash(str(exc), "error")
        return redirect(url_for("public.reference_reply", token=token))
    return render_template("reference.html", ref=ref, RefStatus=RefStatus)


@bp.get("/dev/outbox")
def dev_outbox() -> ResponseReturnValue:
    sms = current_app.extensions["sms"]
    if not current_app.config["DEV_OUTBOX"] or not isinstance(sms, ConsoleSms):
        abort(404)
    return render_template("outbox.html", messages=list(sms.outbox))


@bp.get("/healthz")
def healthz() -> Response:
    return Response("ok", mimetype="text/plain")
