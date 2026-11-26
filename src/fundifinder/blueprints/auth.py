"""Sign up, log in, phone verification."""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from .. import clock, verification
from ..forms import Form
from ..marketplace import RuleViolation
from ..models import FundiProfile, Role, Trade, User
from ..phones import canonical_email
from ..security import (
    client_key,
    hash_password,
    ip_hash,
    login_required,
    login_user,
    logout_user,
    rate_limit,
    safe_next,
    verify_password,
)
from ..web import db, is_htmx, require_user, secret

bp = Blueprint("auth", __name__)

_DUMMY_HASH = hash_password("not-a-real-password", "pbkdf2:sha256:1000")


def home_for(user: User) -> str:
    if user.is_admin:
        return url_for("admin.dashboard")
    if user.is_fundi:
        return url_for("fundi.dashboard")
    return url_for("customer.activity")


@bp.route("/signup", methods=["GET", "POST"])
def signup() -> ResponseReturnValue:
    session = db()
    trades = list(session.scalars(select(Trade).order_by(Trade.name)))
    form: dict[str, Any] = {"role": request.args.get("role", "customer")}
    errors: dict[str, str] = {}
    if request.method == "POST":
        rate_limit("signup", client_key(), 6, 3600)
        f = Form(request.form)
        role = f.choice("role", "role", {Role.CUSTOMER.value, Role.FUNDI.value})
        name = f.text("name", "Your name", 2, 80)
        email = f.email()
        phone = f.phone()
        password = f.password()
        trade_id: int | None = None
        if role == Role.FUNDI.value:
            trade_slug = f.choice("trade", "trade", {t.slug for t in trades})
            trade_id = next((t.id for t in trades if t.slug == trade_slug), None)
        if f.ok:
            canon = canonical_email(email)
            if session.scalar(select(User.id).where(User.email_canonical == canon)):
                f.errors["email"] = "An account with this email already exists. Log in instead."
            if session.scalar(select(User.id).where(User.phone == phone)):
                f.errors["phone"] = "An account with this phone number already exists."
        if f.ok:
            user = User(
                role=role,
                name=name,
                email=email,
                email_canonical=canonical_email(email),
                phone=phone,
                password_hash=hash_password(password, current_app.config["PASSWORD_HASH_METHOD"]),
                created_at=clock.now(),
                signup_ip_hash=ip_hash(client_key(), secret()),
            )
            session.add(user)
            session.flush()
            if role == Role.FUNDI.value and trade_id:
                session.add(
                    FundiProfile(user_id=user.id, trade_id=trade_id, created_at=clock.now())
                )
            session.commit()
            login_user(user)
            flash("Welcome to Fundi Finder. Verify your phone number to get started.", "ok")
            return redirect(url_for("auth.verify_phone"))
        errors = f.errors
        form = {k: v for k, v in request.form.items() if k != "password"}
    return render_template(
        "auth/signup.html",
        form=form,
        errors=errors,
        trade_options=[(t.slug, t.name) for t in trades],
    )


@bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    errors: dict[str, str] = {}
    form: dict[str, Any] = {}
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        rate_limit("login-ip", client_key(), 20, 600)
        rate_limit("login-email", canonical_email(email), 8, 600)
        user = db().scalar(select(User).where(User.email_canonical == canonical_email(email)))
        ok = verify_password(user.password_hash if user else _DUMMY_HASH, password)
        if user and ok and not user.is_suspended:
            login_user(user)
            return redirect(safe_next(request.args.get("next")) or home_for(user))
        if user and ok and user.is_suspended:
            errors["email"] = "This account is suspended. Contact support."
        else:
            errors["email"] = "Email or password is incorrect."
        form = {"email": email}
    return render_template("auth/login.html", form=form, errors=errors)


@bp.post("/logout")
def logout() -> ResponseReturnValue:
    logout_user()
    return redirect(url_for("public.home"))


def _otp_context(sent: bool = False, error: str = "") -> dict[str, Any]:
    user = require_user()
    return {
        "sent": sent,
        "error": error,
        "verified": user.phone_verified_at is not None,
        "dev_outbox": bool(current_app.config["DEV_OUTBOX"])
        and current_app.config["SMS_PROVIDER"] == "console",
    }


def _after_verify() -> str:
    return safe_next(request.args.get("next") or request.form.get("next")) or home_for(
        require_user()
    )


@bp.get("/verify-phone")
@login_required
def verify_phone() -> ResponseReturnValue:
    return render_template("auth/verify_phone.html", **_otp_context())


@bp.post("/verify-phone/send")
@login_required
def send_code() -> ResponseReturnValue:
    user = require_user()
    rate_limit("otp-send", str(user.id), 5, 3600)
    error = ""
    sent = False
    try:
        verification.send_otp(db(), user, current_app.extensions["sms"], secret())
        db().commit()
        sent = True
    except RuleViolation as exc:
        error = str(exc)
    ctx = _otp_context(sent=sent, error=error)
    if is_htmx():
        return render_template("auth/_otp.html", **ctx)
    return render_template("auth/verify_phone.html", **ctx)


@bp.post("/verify-phone/check")
@login_required
def check_code() -> ResponseReturnValue:
    user = require_user()
    rate_limit("otp-check", str(user.id), 12, 3600)
    error = ""
    try:
        if verification.check_otp(db(), user, request.form.get("code", ""), secret()):
            flash("Phone verified.", "ok")
            target = _after_verify()
            if is_htmx():
                resp = make_response("", 204)
                resp.headers["HX-Redirect"] = target
                return resp
            return redirect(target)
        error = "That code is not right. Check the SMS and try again."
    except RuleViolation as exc:
        error = str(exc)
    ctx = _otp_context(sent=True, error=error)
    if is_htmx():
        return render_template("auth/_otp.html", **ctx)
    return render_template("auth/verify_phone.html", **ctx), 400
