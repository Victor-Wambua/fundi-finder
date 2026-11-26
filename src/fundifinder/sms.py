"""Pluggable SMS interface. Only a console provider ships; no real SMS is sent."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import clock

log = logging.getLogger("fundifinder.sms")


@dataclass(frozen=True)
class Message:
    to: str
    body: str
    sent_at: datetime


class SmsProvider(Protocol):
    def send(self, to: str, body: str) -> None: ...


class ConsoleSms:
    """Writes messages to the log and keeps the last 200 in memory (dev outbox)."""

    def __init__(self) -> None:
        self.outbox: deque[Message] = deque(maxlen=200)

    def send(self, to: str, body: str) -> None:
        self.outbox.appendleft(Message(to, body, clock.now()))
        log.info("SMS to %s: %s", to, body)

    def last_for(self, to: str) -> Message | None:
        return next((m for m in self.outbox if m.to == to), None)


class UnknownProvider(ValueError):
    pass


def make_provider(config: dict[str, Any]) -> SmsProvider:
    name = config.get("SMS_PROVIDER", "console")
    if name == "console":
        return ConsoleSms()
    raise UnknownProvider(
        f"SMS provider {name!r} is not implemented. Add an adapter with a send(to, body) method."
    )
