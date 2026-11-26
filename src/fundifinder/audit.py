"""Append-only audit trail for privileged actions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from . import clock
from .models import AuditLog, User


def log(
    db: Session,
    actor: User | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    detail: str = "",
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail[:500],
        created_at=clock.now(),
    )
    db.add(entry)
    return entry
