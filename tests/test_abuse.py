from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from conftest import NOW
from factories import make_catalog, make_fundi, make_job, make_review, make_user
from fundifinder import abuse_scan, queries
from fundifinder.abuse import (
    ReviewerRecord,
    detect_all,
    detect_review_rings,
    detect_self_dealing,
    pair_signals,
)
from fundifinder.models import (
    AbuseFlag,
    FlagStatus,
    JobStatus,
    Review,
    ReviewStatus,
    Role,
    User,
)

T0 = datetime(2026, 3, 1, 9, 0)


def rec(
    uid,
    *,
    fundi=1,
    rating=5,
    created=T0,
    ip="net-a",
    prefix="+2547000009",
    reviews=1,
    review_at=None,
    fundi_ip="fundi-net",
    fundi_prefix="+2547000001",
    fundi_created=None,
):
    return ReviewerRecord(
        review_id=uid,
        user_id=uid,
        fundi_id=fundi,
        rating=rating,
        review_created=review_at or T0 + timedelta(days=30, minutes=uid),
        account_created=created,
        ip_hash=ip,
        phone_prefix=prefix,
        reviewer_review_count=reviews,
        fundi_account_created=fundi_created or T0 - timedelta(days=300),
        fundi_ip_hash=fundi_ip,
        fundi_phone_prefix=fundi_prefix,
    )


# ---- pure heuristics --------------------------------------------------------------------


def test_one_signal_is_not_a_link():
    a = rec(1, created=T0, ip="x", prefix="p1")
    b = rec(2, created=T0 + timedelta(hours=1), ip="y", prefix="p2")
    assert pair_signals(a, b) == {"accounts created within 24h"}


def test_two_signals_link_accounts():
    a, b = rec(1), rec(2, ip="other")
    assert pair_signals(a, b) == {"accounts created within 24h", "same phone prefix"}


def test_three_linked_reviewers_form_a_ring():
    findings = detect_review_rings([rec(i) for i in range(1, 4)])
    assert len(findings) == 1 and findings[0].user_ids == (1, 2, 3)
    assert findings[0].score >= 80


def test_two_linked_reviewers_are_not_a_ring():
    assert detect_review_rings([rec(1), rec(2)]) == []


def test_unlinked_honest_reviewers_are_not_flagged():
    honest = [
        rec(i, created=T0 + timedelta(days=40 * i), ip=f"ip{i}", prefix=f"p{i}")
        for i in range(1, 12)
    ]
    assert detect_all(honest) == []


def test_shared_network_alone_is_not_enough():
    # a whole office behind one mobile NAT
    crowd = [rec(i, created=T0 + timedelta(days=30 * i), prefix=f"p{i}") for i in range(1, 8)]
    assert detect_all(crowd) == []


def test_ring_is_scoped_per_fundi():
    records = [rec(1, fundi=1), rec(2, fundi=1), rec(3, fundi=2), rec(4, fundi=2)]
    assert detect_review_rings(records) == []


def test_chain_of_links_joins_into_one_ring():
    a = rec(1, created=T0, ip="n1", prefix="p1")
    b = rec(2, created=T0 + timedelta(hours=2), ip="n1", prefix="p2")  # links to a (day + ip)
    c = rec(3, created=T0 + timedelta(days=5), ip="n3", prefix="p2")  # links to b? needs 2 signals
    d = rec(4, created=T0 + timedelta(days=5, hours=1), ip="n3", prefix="p2")
    e = rec(5, created=T0 + timedelta(hours=1), ip="n1", prefix="p9")
    found = detect_review_rings([a, b, c, d, e])
    assert [f.user_ids for f in found] == [(1, 2, 5)]


def test_score_rises_with_uniform_high_ratings_and_single_purpose_accounts():
    low = detect_review_rings([rec(i, rating=2, reviews=1) for i in range(1, 4)])[0].score
    high = detect_review_rings([rec(i, rating=5, reviews=1) for i in range(1, 4)])[0].score
    assert high > low


def test_linked_but_established_reviewers_are_not_a_ring():
    # neighbours who share a network and review plenty of other fundis too
    established = [rec(i, reviews=6) for i in range(1, 5)]
    assert detect_review_rings(established) == []
    mixed = [rec(1, reviews=1), rec(2, reviews=6), rec(3, reviews=6)]
    assert detect_review_rings(mixed) == []


def test_signature_is_stable_for_the_same_cluster():
    a = detect_review_rings([rec(i) for i in range(1, 4)])[0]
    b = detect_review_rings([rec(i) for i in reversed(range(1, 4))])[0]
    assert a.signature == b.signature


def test_self_dealing_needs_two_alt_accounts_with_two_signals_each():
    own = dict(
        ip="fundi-net",
        prefix="+2547000001",
        created=T0 - timedelta(days=299),
        fundi_created=T0 - timedelta(days=300),
    )
    assert len(detect_self_dealing([rec(1, **own), rec(2, **own)])) == 1
    assert detect_self_dealing([rec(1, **own)]) == []
    just_ip = dict(own, prefix="other", created=T0)
    assert detect_self_dealing([rec(1, **just_ip), rec(2, **just_ip)]) == []


def test_ring_explained_by_self_dealing_is_not_double_flagged():
    own = dict(
        ip="fundi-net",
        prefix="+2547000001",
        created=T0 - timedelta(days=299),
        fundi_created=T0 - timedelta(days=300),
    )
    findings = detect_all([rec(i, **own) for i in range(1, 4)])
    assert [f.kind for f in findings] == ["self_review"]


# ---- database scan on real rows ---------------------------------------------------------


def _ring_world(db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 1)
    created = NOW - timedelta(days=20)
    accounts = [
        make_user(
            db,
            10 + i,
            created=created + timedelta(minutes=i),
            ip="ringnet",
            phone=f"+25470000090{i}",
        )
        for i in range(4)
    ]
    reviews = []
    for acct in accounts:
        job = make_job(
            db, acct, fundi, cat, JobStatus.COMPLETED, scheduled=NOW - timedelta(days=10)
        )
        reviews.append(make_review(db, job, 5))
    db.commit()
    return fundi, accounts, reviews


def test_scan_flags_a_ring_and_is_idempotent(db):
    fundi, accounts, _ = _ring_world(db)
    first = abuse_scan.scan(db)
    db.commit()
    assert [f.kind for f in first] == ["review_ring", "duplicate_account"]
    assert abuse_scan.scan(db) == []


def test_flagged_reviews_count_a_quarter_in_ranking(db):
    fundi, _, _ = _ring_world(db)
    before = queries.load_cards(db, [fundi])[0].breakdown.effective_reviews
    abuse_scan.scan(db)
    db.commit()
    after = queries.load_cards(db, [fundi])[0].breakdown.effective_reviews
    assert after == before / 4


def test_confirming_a_flag_removes_reviews_and_suspends_accounts(db):
    from factories import make_admin

    fundi, accounts, reviews = _ring_world(db)
    flag = next(f for f in abuse_scan.scan(db) if f.kind == "review_ring")
    admin = make_admin(db)
    db.commit()
    abuse_scan.resolve_flag(db, admin, flag, confirm=True)
    db.commit()
    assert all(db.get(Review, r.id).status == ReviewStatus.REMOVED for r in reviews)
    assert all(db.get(User, a.id).is_suspended for a in accounts)
    assert flag.status == FlagStatus.CONFIRMED
    assert queries.load_cards(db, [fundi])[0].review_count == 0


def test_dismissing_a_flag_changes_nothing_else(db):
    from factories import make_admin

    _, accounts, reviews = _ring_world(db)
    flag = next(f for f in abuse_scan.scan(db) if f.kind == "review_ring")
    admin = make_admin(db)
    abuse_scan.resolve_flag(db, admin, flag, confirm=False)
    db.commit()
    assert flag.status == FlagStatus.DISMISSED
    assert not any(db.get(User, a.id).is_suspended for a in accounts)
    assert all(db.get(Review, r.id).status == ReviewStatus.PUBLISHED for r in reviews)


def test_flags_can_only_be_resolved_once_by_admins(db):
    import pytest

    from factories import make_admin
    from fundifinder.marketplace import RuleViolation

    _, accounts, _ = _ring_world(db)
    flag = next(iter(abuse_scan.scan(db)))
    with pytest.raises(RuleViolation):
        abuse_scan.resolve_flag(db, accounts[0], flag, confirm=True)
    admin = make_admin(db)
    abuse_scan.resolve_flag(db, admin, flag, confirm=False)
    with pytest.raises(RuleViolation):
        abuse_scan.resolve_flag(db, admin, flag, confirm=True)


# ---- seeded data: the planted rings are found, honest data is not -----------------------


def test_seeded_rings_are_all_detected(seeded_db):
    flags = list(seeded_db.scalars(select(AbuseFlag)))
    kinds = sorted(f.kind for f in flags)
    assert kinds.count("review_ring") == 2 and kinds.count("self_review") == 1
    sizes = sorted(len(f.review_id_list) for f in flags if f.kind != "duplicate_account")
    assert sizes == [3, 5, 6]


def test_no_honest_fundi_is_flagged(seeded_db):
    flagged_fundis = {f.fundi_id for f in seeded_db.scalars(select(AbuseFlag)) if f.fundi_id}
    assert len(flagged_fundis) == 3
    for flag in seeded_db.scalars(select(AbuseFlag).where(AbuseFlag.kind != "duplicate_account")):
        reviewers = [seeded_db.get(User, uid) for uid in flag.user_id_list]
        assert all(u is not None and u.role == Role.CUSTOMER for u in reviewers)
        for u in reviewers:
            wrote = list(seeded_db.scalars(select(Review).where(Review.customer_id == u.id)))
            assert len(wrote) == 1 and wrote[0].rating == 5  # single-purpose five-star accounts


def test_flagged_ring_reviews_are_exactly_the_planted_ones(seeded_db):
    ids = {i for f in seeded_db.scalars(select(AbuseFlag)) for i in f.review_id_list}
    rows = list(seeded_db.scalars(select(Review).where(Review.id.in_(ids))))
    assert len(rows) == 14 and all(r.rating == 5 for r in rows)


def test_a_second_scan_of_seed_data_finds_nothing_new(seeded_db):
    assert abuse_scan.scan(seeded_db) == []
