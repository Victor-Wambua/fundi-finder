"""Basic abuse heuristics. Pure functions over plain records.

They flag; they never act. Every finding goes to an admin queue with the evidence listed.
Signals are weak individually (a family shares Wi-Fi, SIMs are sold in blocks), so a link
between two accounts needs at least two signals at once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

SAME_DAY_WINDOW = timedelta(hours=24)
MIN_RING_SIZE = 3
MIN_SELF_DEALING = 2


@dataclass(frozen=True)
class ReviewerRecord:
    review_id: int
    user_id: int
    fundi_id: int
    rating: int
    review_created: datetime
    account_created: datetime
    ip_hash: str | None
    phone_prefix: str
    reviewer_review_count: int  # reviews this account has written, in total
    fundi_account_created: datetime
    fundi_ip_hash: str | None
    fundi_phone_prefix: str


@dataclass(frozen=True)
class Finding:
    kind: str  # review_ring | self_review
    fundi_id: int
    user_ids: tuple[int, ...]
    review_ids: tuple[int, ...]
    score: int
    signals: tuple[str, ...]
    summary: str

    @property
    def signature(self) -> str:
        raw = f"{self.kind}:{self.fundi_id}:{','.join(map(str, self.user_ids))}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


def pair_signals(a: ReviewerRecord, b: ReviewerRecord) -> set[str]:
    signals: set[str] = set()
    if abs(a.account_created - b.account_created) < SAME_DAY_WINDOW:
        signals.add("accounts created within 24h")
    if a.phone_prefix and a.phone_prefix == b.phone_prefix:
        signals.add("same phone prefix")
    if a.ip_hash and a.ip_hash == b.ip_hash:
        signals.add("same sign-up network")
    return signals


def _components(records: Sequence[ReviewerRecord]) -> list[list[ReviewerRecord]]:
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(records):
        for j in range(i + 1, len(records)):
            if len(pair_signals(a, records[j])) >= 2:
                parent[find(i)] = find(j)
    groups: dict[int, list[ReviewerRecord]] = {}
    for i, rec in enumerate(records):
        groups.setdefault(find(i), []).append(rec)
    return list(groups.values())


def detect_review_rings(records: Sequence[ReviewerRecord]) -> list[Finding]:
    """Clusters of >=3 reviewers of one fundi whose accounts look like they came together."""
    by_fundi: dict[int, list[ReviewerRecord]] = {}
    for rec in records:
        by_fundi.setdefault(rec.fundi_id, []).append(rec)
    findings: list[Finding] = []
    for fundi_id, recs in by_fundi.items():
        for comp in _components(recs):
            if len(comp) < MIN_RING_SIZE:
                continue
            signals: set[str] = set()
            for i, a in enumerate(comp):
                for b in comp[i + 1 :]:
                    signals |= pair_signals(a, b)
            score = 40 + 10 * min(len(comp) - MIN_RING_SIZE, 4)
            if all(r.rating >= 4 for r in comp):
                score += 15
            single = sum(1 for r in comp if r.reviewer_review_count == 1)
            score += round(15 * single / len(comp))
            span = max(r.review_created for r in comp) - min(r.review_created for r in comp)
            if span <= timedelta(days=7):
                score += 10
            ordered = sorted(signals)
            findings.append(
                Finding(
                    kind="review_ring",
                    fundi_id=fundi_id,
                    user_ids=tuple(sorted(r.user_id for r in comp)),
                    review_ids=tuple(sorted(r.review_id for r in comp)),
                    score=min(100, score),
                    signals=tuple(ordered),
                    summary=(
                        f"{len(comp)} reviewers with linked accounts ({', '.join(ordered)}); "
                        f"{single} wrote no other review"
                    ),
                )
            )
    return findings


def detect_self_dealing(records: Sequence[ReviewerRecord]) -> list[Finding]:
    """Reviewers who look like the fundi's own other accounts."""
    by_fundi: dict[int, list[tuple[ReviewerRecord, set[str]]]] = {}
    for r in records:
        signals: set[str] = set()
        if r.ip_hash and r.ip_hash == r.fundi_ip_hash:
            signals.add("same sign-up network as the fundi")
        if r.phone_prefix and r.phone_prefix == r.fundi_phone_prefix:
            signals.add("same phone prefix as the fundi")
        if abs(r.account_created - r.fundi_account_created) < SAME_DAY_WINDOW:
            signals.add("created the same day as the fundi")
        if len(signals) >= 2:
            by_fundi.setdefault(r.fundi_id, []).append((r, signals))
    findings: list[Finding] = []
    for fundi_id, items in by_fundi.items():
        if len(items) < MIN_SELF_DEALING:
            continue
        all_signals = sorted({s for _, sig in items for s in sig})
        recs = [r for r, _ in items]
        score = min(
            100,
            55
            + 10 * min(len(recs) - MIN_SELF_DEALING, 4)
            + (15 if all(r.rating >= 4 for r in recs) else 0),
        )
        findings.append(
            Finding(
                kind="self_review",
                fundi_id=fundi_id,
                user_ids=tuple(sorted(r.user_id for r in recs)),
                review_ids=tuple(sorted(r.review_id for r in recs)),
                score=score,
                signals=tuple(all_signals),
                summary=f"{len(recs)} reviewers look like the fundi's own accounts ({', '.join(all_signals)})",
            )
        )
    return findings


def detect_all(records: Sequence[ReviewerRecord]) -> list[Finding]:
    """Both detectors, with a ring dropped when self-dealing already explains the same reviews."""
    self_dealing = detect_self_dealing(records)
    covered = [set(f.review_ids) for f in self_dealing]
    rings = [
        f for f in detect_review_rings(records) if not any(set(f.review_ids) <= c for c in covered)
    ]
    return rings + self_dealing
