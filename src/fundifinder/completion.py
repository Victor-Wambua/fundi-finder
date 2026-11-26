"""One-time completion codes.

The code is derived (HMAC) from a per-job random salt and the server secret, so it is never
stored in clear and can be re-displayed to the customer. It is single use, wrong guesses are
rate limited, and after too many wrong guesses it is invalidated until an admin re-issues it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .models import Job

MAX_FAILURES_PER_WINDOW = 5
LOCKOUT = timedelta(hours=1)
MAX_TOTAL_FAILURES = 10
CODE_DIGITS = 6


class CodeResult(StrEnum):
    OK = "ok"
    WRONG = "wrong"
    LOCKED = "locked"
    INVALIDATED = "invalidated"
    USED = "used"


@dataclass(frozen=True)
class CodeCheck:
    result: CodeResult
    attempts_left: int = 0
    locked_until: datetime | None = None


def new_salt() -> str:
    return secrets.token_hex(16)


def derive_code(secret: str, salt: str) -> str:
    digest = hmac.new(secret.encode(), salt.encode(), hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big") % (10**CODE_DIGITS)
    return f"{number:0{CODE_DIGITS}d}"


def normalise(supplied: str) -> str:
    return "".join(ch for ch in supplied if ch.isdigit())


def check_code(job: Job, supplied: str, secret: str, now: datetime) -> CodeCheck:
    """Validate ``supplied`` against the job's code and update the failure counters in place.

    On OK the code is marked used (single use). The caller must commit the session either way:
    failure counters must persist or the lockout would be bypassable.
    """
    if job.code_used_at is not None:
        return CodeCheck(CodeResult.USED)
    if job.code_invalidated:
        return CodeCheck(CodeResult.INVALIDATED)
    if job.code_locked_until is not None and now < job.code_locked_until:
        return CodeCheck(CodeResult.LOCKED, locked_until=job.code_locked_until)

    expected = derive_code(secret, job.code_salt)
    if hmac.compare_digest(normalise(supplied).encode(), expected.encode()):
        job.code_used_at = now
        job.code_failures = 0
        return CodeCheck(CodeResult.OK)

    job.code_failures += 1
    job.code_total_failures += 1
    if job.code_total_failures >= MAX_TOTAL_FAILURES:
        job.code_invalidated = True
        return CodeCheck(CodeResult.INVALIDATED)
    if job.code_failures >= MAX_FAILURES_PER_WINDOW:
        job.code_failures = 0
        job.code_locked_until = now + LOCKOUT
        return CodeCheck(CodeResult.LOCKED, locked_until=job.code_locked_until)
    return CodeCheck(CodeResult.WRONG, attempts_left=MAX_FAILURES_PER_WINDOW - job.code_failures)


def reissue(job: Job) -> None:
    """Admin action: new salt (so a new code) and cleared counters. Used codes stay used."""
    job.code_salt = new_salt()
    job.code_failures = 0
    job.code_total_failures = 0
    job.code_locked_until = None
    job.code_invalidated = False
