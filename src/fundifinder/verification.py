"""Phone OTP, ID document review and customer references."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, clock
from .marketplace import RuleViolation
from .models import (
    FundiProfile,
    IdDocument,
    IdStatus,
    PhoneOtp,
    RefStatus,
    Reference,
    Role,
    User,
)
from .phones import normalise_phone
from .sms import SmsProvider
from .uploads import Storage, store_upload

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 3
MAX_REFERENCES = 5


# ---- phone OTP -------------------------------------------------------------------------


def _otp_hash(secret: str, user_id: int, code: str) -> str:
    return hmac.new(secret.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def send_otp(db: Session, user: User, sms: SmsProvider, secret: str) -> None:
    now = clock.now()
    if user.phone_verified_at is not None:
        raise RuleViolation("Your phone is already verified.")
    recent = db.scalar(
        select(func.count())
        .select_from(PhoneOtp)
        .where(PhoneOtp.user_id == user.id, PhoneOtp.created_at > now - timedelta(hours=1))
    )
    if (recent or 0) >= OTP_MAX_SENDS_PER_HOUR:
        raise RuleViolation("Too many codes requested. Try again in an hour.")
    for old in db.scalars(
        select(PhoneOtp).where(PhoneOtp.user_id == user.id, PhoneOtp.consumed_at.is_(None))
    ):
        old.consumed_at = now  # only the newest code works
    code = f"{secrets.randbelow(10**6):06d}"
    db.add(
        PhoneOtp(
            user_id=user.id,
            code_hash=_otp_hash(secret, user.id, code),
            created_at=now,
            expires_at=now + OTP_TTL,
        )
    )
    sms.send(user.phone, f"Fundi Finder: your verification code is {code}. It expires in 10 minutes.")


def check_otp(db: Session, user: User, code: str, secret: str) -> bool:
    """True and the phone is marked verified if the code matches. Commits counters either way."""
    now = clock.now()
    otp = db.scalar(
        select(PhoneOtp)
        .where(PhoneOtp.user_id == user.id, PhoneOtp.consumed_at.is_(None))
        .order_by(PhoneOtp.id.desc())
    )
    if otp is None or now > otp.expires_at:
        raise RuleViolation("That code has expired. Request a new one.")
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        raise RuleViolation("Too many wrong attempts. Request a new code.")
    otp.attempts += 1
    supplied = "".join(ch for ch in code if ch.isdigit())
    if hmac.compare_digest(_otp_hash(secret, user.id, supplied), otp.code_hash):
        otp.consumed_at = now
        user.phone_verified_at = now
        db.commit()
        return True
    db.commit()
    return False


# ---- ID documents ----------------------------------------------------------------------


def submit_id_document(
    db: Session, profile: FundiProfile, data: bytes, filename: str | None, storage: Storage,
    max_bytes: int,
) -> IdDocument:
    pending = db.scalar(
        select(IdDocument).where(
            IdDocument.fundi_id == profile.id, IdDocument.status == IdStatus.PENDING
        )
    )
    if pending:
        raise RuleViolation("You already have a document waiting for review.")
    approved = db.scalar(
        select(IdDocument).where(
            IdDocument.fundi_id == profile.id, IdDocument.status == IdStatus.APPROVED
        )
    )
    if approved:
        raise RuleViolation("Your ID has already been checked.")
    name = store_upload(storage, "private", data, filename, max_bytes)
    doc = IdDocument(fundi_id=profile.id, storage_name=name, created_at=clock.now())
    db.add(doc)
    db.flush()
    return doc


def review_id_document(db: Session, admin: User, doc: IdDocument, approve: bool, note: str) -> None:
    if admin.role != Role.ADMIN:
        raise RuleViolation("Only admins can review identity documents.")
    if doc.status != IdStatus.PENDING:
        raise RuleViolation("This document was already reviewed.")
    if not approve and not note.strip():
        raise RuleViolation("Say why the document was rejected so the fundi can fix it.")
    doc.status = IdStatus.APPROVED if approve else IdStatus.REJECTED
    doc.reviewed_by_id = admin.id
    doc.reviewed_at = clock.now()
    doc.review_note = note.strip()[:300]
    audit.log(
        db,
        admin,
        "id.approved" if approve else "id.rejected",
        "fundi",
        doc.fundi_id,
        note,
    )


# ---- references ------------------------------------------------------------------------


def add_reference(
    db: Session, profile: FundiProfile, name: str, phone_raw: str, note: str,
    sms: SmsProvider, base_url: str,
) -> Reference:
    phone = normalise_phone(phone_raw)
    if phone is None:
        raise RuleViolation("Enter the reference's Kenyan mobile number.")
    if phone == profile.user.phone:
        raise RuleViolation("A reference cannot be your own number.")
    refs = list(db.scalars(select(Reference).where(Reference.fundi_id == profile.id)))
    if len(refs) >= MAX_REFERENCES:
        raise RuleViolation("You have added the maximum number of references.")
    if any(r.phone == phone for r in refs):
        raise RuleViolation("You already added that number.")
    ref = Reference(
        fundi_id=profile.id,
        name=name,
        phone=phone,
        relationship_note=note[:120],
        token=secrets.token_urlsafe(24),
        created_at=clock.now(),
    )
    db.add(ref)
    db.flush()
    sms.send(
        phone,
        f"{profile.user.name} listed you as a reference on Fundi Finder. "
        f"Did they work for you? Reply here: {base_url}/ref/{ref.token}",
    )
    return ref


def respond_to_reference(db: Session, ref: Reference, confirm: bool) -> None:
    if ref.status != RefStatus.PENDING:
        raise RuleViolation("This reference was already answered.")
    ref.status = RefStatus.CONFIRMED if confirm else RefStatus.DECLINED
    ref.confirmed_at = clock.now() if confirm else None
