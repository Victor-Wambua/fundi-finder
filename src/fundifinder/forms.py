"""Small, explicit form validation helpers (no form library)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import date, datetime

from .phones import normalise_phone

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Form:
    def __init__(self, data: Mapping[str, str]) -> None:
        self.data = data
        self.errors: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return not self.errors

    def _raw(self, name: str) -> str:
        return (self.data.get(name) or "").strip()

    def text(
        self, name: str, label: str, min_len: int = 1, max_len: int = 200, required: bool = True
    ) -> str:
        value = self._raw(name)
        if not value:
            if required:
                self.errors[name] = f"{label} is required."
            return ""
        if len(value) < min_len:
            self.errors[name] = f"{label} must be at least {min_len} characters."
        elif len(value) > max_len:
            self.errors[name] = f"{label} must be at most {max_len} characters."
        return value

    def email(self, name: str = "email") -> str:
        value = self._raw(name).lower()
        if not _EMAIL.match(value) or len(value) > 200:
            self.errors[name] = "Enter a valid email address."
        return value

    def phone(self, name: str = "phone") -> str:
        value = normalise_phone(self._raw(name))
        if value is None:
            self.errors[name] = "Enter a Kenyan mobile number, like 0700 000 001."
            return ""
        return value

    def password(self, name: str = "password", min_len: int = 8) -> str:
        value = self.data.get(name) or ""
        if len(value) < min_len:
            self.errors[name] = f"Use at least {min_len} characters."
        elif len(value) > 200:
            self.errors[name] = "That password is too long."
        return value

    def integer(
        self, name: str, label: str, min_value: int, max_value: int, required: bool = True
    ) -> int | None:
        raw = self._raw(name).replace(",", "")
        if not raw:
            if required:
                self.errors[name] = f"{label} is required."
            return None
        try:
            value = int(raw)
        except ValueError:
            self.errors[name] = f"{label} must be a whole number."
            return None
        if not min_value <= value <= max_value:
            self.errors[name] = f"{label} must be between {min_value:,} and {max_value:,}."
            return None
        return value

    def choice(self, name: str, label: str, allowed: Collection[str]) -> str:
        value = self._raw(name)
        if value not in allowed:
            self.errors[name] = f"Choose a valid {label.lower()}."
            return ""
        return value

    def datetime_local(self, name: str, label: str) -> datetime | None:
        raw = self._raw(name)
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            self.errors[name] = f"Choose a date and time for {label.lower()}."
            return None

    def date_value(self, name: str, label: str) -> date | None:
        raw = self._raw(name)
        try:
            return date.fromisoformat(raw)
        except ValueError:
            self.errors[name] = f"Choose a date for {label.lower()}."
            return None
