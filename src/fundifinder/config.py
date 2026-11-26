"""Application configuration, read from FUNDI_* environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def default_config() -> dict[str, Any]:
    instance = Path(os.environ.get("FUNDI_INSTANCE_DIR", ROOT / "instance"))
    return {
        "SECRET_KEY": os.environ.get("FUNDI_SECRET_KEY", "dev-only-secret-change-me"),
        "INSTANCE_DIR": str(instance),
        "DATABASE_URL": os.environ.get(
            "FUNDI_DATABASE_URL", f"sqlite:///{instance / 'fundifinder.db'}"
        ),
        "SMS_PROVIDER": os.environ.get("FUNDI_SMS_PROVIDER", "console"),
        "DEV_OUTBOX": _flag("FUNDI_DEV_OUTBOX", True),
        "RATELIMIT_ENABLED": _flag("FUNDI_RATELIMIT_ENABLED", True),
        "PASSWORD_HASH_METHOD": "scrypt",
        "MAX_UPLOAD_BYTES": 5 * 1024 * 1024,
        "MAX_CONTENT_LENGTH": 6 * 1024 * 1024,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _flag("FUNDI_COOKIE_SECURE", False),
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * 24 * 14,
        "TESTING": False,
    }
