"""CSRF, passwords, sessions, rate limiting, security headers, auth decorators."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from flask import Flask, Response, abort, current_app, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import clock
from .db import get_db
from .models import User

P = ParamSpec("P")
R = TypeVar("R")

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ---- passwords -------------------------------------------------------------------------


def hash_password(password: str, method: str = "scrypt") -> str:
    return generate_password_hash(password, method=method)


def verify_password(stored: str, password: str) -> bool:
    return check_password_hash(stored, password)


# ---- CSRF ------------------------------------------------------------------------------


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return str(token)


def _check_csrf() -> None:
    if request.method in _SAFE_METHODS:
        return
    expected = session.get("csrf", "")
    supplied = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER, "")
    if not expected or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="Your session expired or the form was tampered with. Reload and try again.")


# ---- rate limiting ---------------------------------------------------------------------


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class RateLimiter:
    """In-memory sliding window. Per-process: fine for one worker, not for a fleet."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> int:
        """Record a hit. Returns 0 if allowed, else seconds until the next slot frees up."""
        now = clock.now().timestamp()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(hits[0] + window_seconds - now) + 1)
            hits.append(now)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def rate_limit(name: str, key: str, limit: int, window_seconds: int) -> None:
    """Raise RateLimited when ``key`` exceeds ``limit`` hits per window for bucket ``name``."""
    if not current_app.config["RATELIMIT_ENABLED"]:
        return
    limiter: RateLimiter = current_app.extensions["ratelimiter"]
    wait = limiter.check(f"{name}:{key}", limit, window_seconds)
    if wait:
        raise RateLimited(wait)


def ip_hash(ip: str | None, secret: str) -> str:
    """Keyed hash of an IP so the abuse heuristics can compare networks without storing IPs."""
    return hmac.new(secret.encode(), (ip or "unknown").encode(), hashlib.sha256).hexdigest()[:16]


def client_key() -> str:
    return request.remote_addr or "unknown"


# ---- auth ------------------------------------------------------------------------------


def current_user() -> User | None:
    if "user" in g:
        user: User | None = g.user
        return user
    user = None
    uid = session.get("uid")
    if uid is not None:
        user = get_db().get(User, uid)
        if user is None or user.is_suspended:
            session.clear()
            user = None
    g.user = user
    return user


def login_user(user: User) -> None:
    session.clear()  # new session: defeats fixation
    session["uid"] = user.id
    session.permanent = True
    g.user = user


def logout_user() -> None:
    session.clear()
    g.user = None


def login_required(view: Callable[P, R]) -> Callable[P, R | Response]:
    @wraps(view)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R | Response:
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles: str) -> Callable[[Callable[P, R]], Callable[P, R | Response]]:
    def decorator(view: Callable[P, R]) -> Callable[P, R | Response]:
        @wraps(view)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R | Response:
            user = current_user()
            if user is None:
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            if user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def safe_next(target: str | None) -> str | None:
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ---- wiring ----------------------------------------------------------------------------

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def init_security(app: Flask) -> None:
    app.extensions["ratelimiter"] = RateLimiter()
    app.before_request(_check_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.after_request
    def _headers(resp: Response) -> Response:
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.endpoint and request.endpoint != "static" and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(RateLimited)
    def _limited(exc: RateLimited) -> tuple[str, int, dict[str, str]]:
        from flask import render_template

        return (
            render_template("error.html", code=429, title="Slow down a little",
                            message=f"Too many attempts. Try again in about {exc.retry_after} seconds."),
            429,
            {"Retry-After": str(exc.retry_after)},
        )

