"""Kenyan phone number handling (E.164, +254...)."""

from __future__ import annotations

import re

_LOCAL = re.compile(r"^0([17]\d{8})$")
_INTL = re.compile(r"^\+?254([17]\d{8})$")


def normalise_phone(raw: str) -> str | None:
    """Return +254XXXXXXXXX for a valid Kenyan mobile number, else None."""
    cleaned = re.sub(r"[\s\-().]", "", raw or "")
    m = _LOCAL.match(cleaned) or _INTL.match(cleaned)
    if not m:
        return None
    return f"+254{m.group(1)}"


def phone_prefix(phone: str) -> str:
    """Prefix used by the abuse heuristics: everything but the last two digits.

    SIMs are often sold in consecutive blocks, so accounts that share this prefix and were
    created together deserve a second look. Alone it proves nothing.
    """
    return phone[:-2]


def display_phone(phone: str) -> str:
    if phone.startswith("+254") and len(phone) == 13:
        local = "0" + phone[4:]
        return f"{local[:4]} {local[4:7]} {local[7:]}"
    return phone


def whatsapp_url(phone: str, text: str = "") -> str:
    from urllib.parse import quote

    base = f"https://wa.me/{phone.lstrip('+')}"
    return f"{base}?text={quote(text)}" if text else base


def tel_url(phone: str) -> str:
    return f"tel:{phone}"


def canonical_email(email: str) -> str:
    """Lower-cased email with +tags removed (and dots removed for Gmail) for duplicate checks."""
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"
