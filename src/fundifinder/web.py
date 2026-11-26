"""Small helpers shared by the blueprints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar

from flask import abort, current_app, flash, request
from sqlalchemy.orm import Session

from .db import get_db
from .marketplace import RuleViolation
from .models import User
from .security import current_user
from .uploads import Storage, UploadError, store_upload

T = TypeVar("T")


def storage() -> Storage:
    s: Storage = current_app.extensions["storage"]
    return s


def secret() -> str:
    return str(current_app.config["SECRET_KEY"])


def get_or_404(db: Session, model: type[T], ident: int) -> T:
    obj = db.get(model, ident)
    if obj is None:
        abort(404)
    return obj


def require_user() -> User:
    user = current_user()
    if user is None:  # decorators make this unreachable; keeps the type checker honest
        abort(401)
    return user


def is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def kes(amount: int | None, bare: bool = False) -> str:
    if amount is None:
        return "—"
    return f"{amount:,}" if bare else f"KES {amount:,}"


def response_label(hours: float) -> str:
    if hours < 1:
        return "an hour"
    if hours < 24:
        n = round(hours)
        return f"{n} hour{'' if n == 1 else 's'}"
    days = round(hours / 24)
    return f"{days} day{'' if days == 1 else 's'}"


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%a %d %b %Y, %H:%M") if value else "—"


def fmt_date(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def fmt_dt_input(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def reviewer_label(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


def read_upload(field: str, required: bool = False) -> tuple[bytes, str | None] | None:
    file = request.files.get(field)
    if file is None or not file.filename:
        return None
    max_bytes = int(current_app.config["MAX_UPLOAD_BYTES"])
    data = file.stream.read(max_bytes + 1)
    return data, file.filename


def save_upload_or_flash(field: str, zone: str) -> tuple[str | None, bool]:
    """Returns (stored_name, ok). ok is False when an upload was supplied but rejected."""
    got = read_upload(field)
    if got is None:
        return None, True
    data, filename = got
    try:
        name = store_upload(
            storage(), zone, data, filename, int(current_app.config["MAX_UPLOAD_BYTES"])
        )
    except UploadError as exc:
        flash(str(exc), "error")
        return None, False
    return name, True


def rule_error(exc: RuleViolation) -> None:
    flash(str(exc), "error")


def db() -> Session:
    return get_db()


def template_context() -> dict[str, Any]:
    return {"user": current_user()}
