"""Admin console: verification queue, reports and appeals, abuse flags, users, audit log."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from .. import abuse_scan, audit, completion, moderation, verification
from ..marketplace import RuleViolation
from ..models import (
    AbuseFlag,
    AppealStatus,
    AuditLog,
    FlagStatus,
    FundiProfile,
    IdDocument,
    IdStatus,
    Job,
    JobStatus,
    Report,
    ReportStatus,
    Review,
    ReviewStatus,
    Role,
    User,
)
from ..security import role_required
from ..uploads import mime_for
from ..web import db, get_or_404, require_user, rule_error, storage

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/")
@role_required(Role.ADMIN)
def dashboard() -> ResponseReturnValue:
    s = db()

    def count(stmt: object) -> int:
        return int(s.scalar(stmt) or 0)  # type: ignore[call-overload]

    stats = {
        "customers": count(
            select(func.count()).select_from(User).where(User.role == Role.CUSTOMER)
        ),
        "fundis": count(select(func.count()).select_from(FundiProfile)),
        "suspended": count(
            select(func.count()).select_from(User).where(User.is_suspended.is_(True))
        ),
        "jobs_total": count(select(func.count()).select_from(Job)),
        "jobs_completed": count(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.COMPLETED)
        ),
        "reviews": count(
            select(func.count()).select_from(Review).where(Review.status == ReviewStatus.PUBLISHED)
        ),
        "removed": count(
            select(func.count()).select_from(Review).where(Review.status == ReviewStatus.REMOVED)
        ),
        "avg_rating": float(
            s.scalar(select(func.avg(Review.rating)).where(Review.status == ReviewStatus.PUBLISHED))
            or 0
        ),
    }
    queues = {
        "ids": count(
            select(func.count())
            .select_from(IdDocument)
            .where(IdDocument.status == IdStatus.PENDING)
        ),
        "reports": count(
            select(func.count()).select_from(Report).where(Report.status == ReportStatus.OPEN)
        ),
        "appeals": count(
            select(func.count())
            .select_from(Report)
            .where(Report.appeal_status == AppealStatus.PENDING)
        ),
        "flags": count(
            select(func.count()).select_from(AbuseFlag).where(AbuseFlag.status == FlagStatus.OPEN)
        ),
        "locked": count(
            select(func.count()).select_from(Job).where(Job.code_invalidated.is_(True))
        ),
    }
    by_status = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return render_template("admin/dashboard.html", stats=stats, queues=queues, by_status=by_status)


# ---- verification queue ----------------------------------------------------------------


@bp.get("/verification")
@role_required(Role.ADMIN)
def verification_queue() -> ResponseReturnValue:
    s = db()
    pending = list(
        s.scalars(
            select(IdDocument)
            .where(IdDocument.status == IdStatus.PENDING)
            .options(selectinload(IdDocument.fundi).selectinload(FundiProfile.user))
            .order_by(IdDocument.created_at)
        )
    )
    recent = list(
        s.scalars(
            select(IdDocument)
            .where(IdDocument.status != IdStatus.PENDING)
            .options(selectinload(IdDocument.fundi).selectinload(FundiProfile.user))
            .order_by(IdDocument.reviewed_at.desc())
            .limit(15)
        )
    )
    return render_template("admin/verification.html", pending=pending, recent=recent)


@bp.get("/verification/<int:doc_id>")
@role_required(Role.ADMIN)
def verification_detail(doc_id: int) -> ResponseReturnValue:
    doc = get_or_404(db(), IdDocument, doc_id)
    return render_template("admin/verification_detail.html", doc=doc, IdStatus=IdStatus)


@bp.get("/documents/<int:doc_id>/file")
@role_required(Role.ADMIN)
def document_file(doc_id: int) -> ResponseReturnValue:
    """The only route that serves an ID document. Admin only, never cached, always logged."""
    doc = get_or_404(db(), IdDocument, doc_id)
    path = storage().path("private", doc.storage_name)
    if path is None:
        abort(404)
    audit.log(db(), require_user(), "id.viewed", "fundi", doc.fundi_id, f"document #{doc.id}")
    db().commit()
    resp = send_file(path, mimetype=mime_for(doc.storage_name))
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Content-Disposition"] = "inline"
    return resp


@bp.post("/verification/<int:doc_id>/decide")
@role_required(Role.ADMIN)
def verification_decide(doc_id: int) -> ResponseReturnValue:
    doc = get_or_404(db(), IdDocument, doc_id)
    approve = request.form.get("decision") == "approve"
    try:
        verification.review_id_document(
            db(), require_user(), doc, approve, request.form.get("note", "")
        )
        db().commit()
        flash("Document approved." if approve else "Document rejected.", "ok")
        return redirect(url_for("admin.verification_queue"))
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
        return redirect(url_for("admin.verification_detail", doc_id=doc.id))


# ---- reports and appeals ---------------------------------------------------------------


@bp.get("/reports")
@role_required(Role.ADMIN)
def reports() -> ResponseReturnValue:
    s = db()
    opts = (
        selectinload(Report.review).selectinload(Review.customer),
        selectinload(Report.review).selectinload(Review.fundi).selectinload(FundiProfile.user),
        selectinload(Report.reporter),
    )
    open_reports = list(
        s.scalars(
            select(Report)
            .where(Report.status == ReportStatus.OPEN)
            .options(*opts)
            .order_by(Report.created_at)
        )
    )
    appeals = list(
        s.scalars(
            select(Report)
            .where(Report.appeal_status == AppealStatus.PENDING)
            .options(*opts)
            .order_by(Report.appeal_at)
        )
    )
    recent = list(
        s.scalars(
            select(Report)
            .where(Report.status != ReportStatus.OPEN)
            .options(*opts)
            .order_by(Report.resolved_at.desc())
            .limit(10)
        )
    )
    return render_template(
        "admin/reports.html",
        open_reports=open_reports,
        appeals=appeals,
        recent=recent,
        reasons=moderation.REASONS,
    )


@bp.post("/reports/<int:report_id>/resolve")
@role_required(Role.ADMIN)
def report_resolve(report_id: int) -> ResponseReturnValue:
    report = get_or_404(db(), Report, report_id)
    uphold = request.form.get("decision") == "uphold"
    try:
        moderation.resolve_report(
            db(), require_user(), report, uphold, request.form.get("note", "")
        )
        db().commit()
        flash("Content removed and the author can appeal." if uphold else "Report dismissed.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("admin.reports"))


@bp.post("/reports/<int:report_id>/appeal")
@role_required(Role.ADMIN)
def appeal_decide(report_id: int) -> ResponseReturnValue:
    report = get_or_404(db(), Report, report_id)
    accept = request.form.get("decision") == "accept"
    try:
        moderation.decide_appeal(db(), require_user(), report, accept, request.form.get("note", ""))
        db().commit()
        flash(
            "Appeal accepted: content restored."
            if accept
            else "Appeal rejected: the takedown stands.",
            "ok",
        )
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("admin.reports"))


# ---- abuse flags -----------------------------------------------------------------------


@bp.get("/abuse")
@role_required(Role.ADMIN)
def abuse() -> ResponseReturnValue:
    s = db()
    flags = list(
        s.scalars(
            select(AbuseFlag)
            .options(selectinload(AbuseFlag.fundi).selectinload(FundiProfile.user))
            .order_by((AbuseFlag.status == FlagStatus.OPEN).desc(), AbuseFlag.score.desc())
        )
    )
    users = {
        u.id: u
        for u in s.scalars(
            select(User).where(User.id.in_({i for f in flags for i in f.user_id_list}))
        )
    }
    return render_template("admin/abuse.html", flags=flags, users=users, FlagStatus=FlagStatus)


@bp.post("/abuse/scan")
@role_required(Role.ADMIN)
def abuse_scan_run() -> ResponseReturnValue:
    created = abuse_scan.scan(db(), require_user())
    db().commit()
    flash(f"Scan finished: {len(created)} new flag(s).", "ok")
    return redirect(url_for("admin.abuse"))


@bp.post("/abuse/<int:flag_id>/resolve")
@role_required(Role.ADMIN)
def abuse_resolve(flag_id: int) -> ResponseReturnValue:
    flag = get_or_404(db(), AbuseFlag, flag_id)
    try:
        abuse_scan.resolve_flag(
            db(), require_user(), flag, request.form.get("decision") == "confirm"
        )
        db().commit()
        flash("Flag resolved.", "ok")
    except RuleViolation as exc:
        db().rollback()
        rule_error(exc)
    return redirect(url_for("admin.abuse"))


# ---- users -----------------------------------------------------------------------------


@bp.get("/users")
@role_required(Role.ADMIN)
def users() -> ResponseReturnValue:
    q = (request.args.get("q") or "").strip()
    role = request.args.get("role") or ""
    stmt = select(User).order_by(User.created_at.desc()).limit(100)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(User.name.ilike(like), User.email.ilike(like), User.phone.ilike(like))
        )
    if role in {r.value for r in Role}:
        stmt = stmt.where(User.role == role)
    return render_template("admin/users.html", items=list(db().scalars(stmt)), q=q, role=role)


@bp.post("/users/<int:user_id>/suspend")
@role_required(Role.ADMIN)
def user_suspend(user_id: int) -> ResponseReturnValue:
    target = get_or_404(db(), User, user_id)
    admin = require_user()
    if target.id == admin.id or target.role == Role.ADMIN:
        flash("Admin accounts cannot be suspended here.", "error")
        return redirect(url_for("admin.users"))
    suspend = request.form.get("action") == "suspend"
    target.is_suspended = suspend
    target.suspended_reason = (request.form.get("reason") or "")[:200] if suspend else None
    audit.log(
        db(),
        admin,
        "user.suspended" if suspend else "user.reinstated",
        "user",
        target.id,
        target.suspended_reason or "",
    )
    db().commit()
    flash(f"{target.name} {'suspended' if suspend else 'reinstated'}.", "ok")
    return redirect(url_for("admin.users"))


# ---- locked completion codes -----------------------------------------------------------


@bp.get("/locked-codes")
@role_required(Role.ADMIN)
def locked_codes() -> ResponseReturnValue:
    items = list(
        db().scalars(
            select(Job)
            .where(Job.code_invalidated.is_(True), Job.code_used_at.is_(None))
            .options(
                selectinload(Job.customer), selectinload(Job.fundi).selectinload(FundiProfile.user)
            )
        )
    )
    return render_template("admin/locked_codes.html", items=items)


@bp.post("/locked-codes/<int:job_id>/reissue")
@role_required(Role.ADMIN)
def reissue(job_id: int) -> ResponseReturnValue:
    job = get_or_404(db(), Job, job_id)
    if job.code_used_at is not None:
        flash("That code was already used.", "error")
    else:
        completion.reissue(job)
        audit.log(
            db(), require_user(), "code.reissued", "job", job.id, "after repeated wrong attempts"
        )
        db().commit()
        flash("A new completion code was issued to the customer.", "ok")
    return redirect(url_for("admin.locked_codes"))


# ---- audit log -------------------------------------------------------------------------


@bp.get("/audit")
@role_required(Role.ADMIN)
def audit_log() -> ResponseReturnValue:
    entries = list(
        db().scalars(
            select(AuditLog)
            .options(selectinload(AuditLog.actor))
            .order_by(AuditLog.id.desc())
            .limit(200)
        )
    )
    return render_template("admin/audit.html", entries=entries)
