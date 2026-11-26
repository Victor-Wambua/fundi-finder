"""Verification badges, computed from recorded facts only. Nobody can self-declare one."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MIN_REFERENCES = 2
MIN_PORTFOLIO_PHOTOS = 3


class BadgeKey(StrEnum):
    PHONE = "phone"
    ID = "id"
    REFERENCES = "references"
    SKILLS = "skills"


@dataclass(frozen=True)
class Badge:
    key: BadgeKey
    label: str
    earned_because: str


@dataclass(frozen=True)
class VerificationFacts:
    phone_verified: bool = False
    id_document_approved: bool = False
    confirmed_reference_phones: tuple[str, ...] = ()
    own_phone: str = ""
    portfolio_photo_count: int = 0


def distinct_reference_count(facts: VerificationFacts) -> int:
    """Confirmed references from distinct phone numbers, excluding the fundi's own."""
    return len({p for p in facts.confirmed_reference_phones if p and p != facts.own_phone})


def derive_badges(facts: VerificationFacts) -> list[Badge]:
    badges: list[Badge] = []
    if facts.phone_verified:
        badges.append(Badge(BadgeKey.PHONE, "Phone verified", "Entered a one-time code sent by SMS"))
    if facts.id_document_approved:
        badges.append(
            Badge(BadgeKey.ID, "ID checked", "An admin reviewed an identity document and approved it")
        )
    if distinct_reference_count(facts) >= MIN_REFERENCES:
        badges.append(
            Badge(
                BadgeKey.REFERENCES,
                "References",
                f"{MIN_REFERENCES}+ past customers confirmed they hired this fundi",
            )
        )
    if facts.portfolio_photo_count >= MIN_PORTFOLIO_PHOTOS:
        badges.append(
            Badge(
                BadgeKey.SKILLS,
                "Trade skills",
                f"{MIN_PORTFOLIO_PHOTOS}+ portfolio photos of their own work",
            )
        )
    return badges
