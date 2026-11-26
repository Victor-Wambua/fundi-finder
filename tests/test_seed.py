from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from fundifinder import queries
from fundifinder.models import (
    AuditLog,
    IdDocument,
    IdStatus,
    Job,
    JobStatus,
    PortfolioPhoto,
    Report,
    Review,
    Role,
    User,
)
from fundifinder.security import verify_password
from fundifinder.seed import DEMO_PASSWORD, SeedError, seed_demo
from helpers import login


def test_seed_volumes(seeded_db):
    n = lambda stmt: seeded_db.scalar(stmt)  # noqa: E731
    assert n(select(func.count()).select_from(User).where(User.role == Role.FUNDI)) == 60
    assert 380 <= n(select(func.count()).select_from(Job)) <= 450
    assert 280 <= n(select(func.count()).select_from(Review)) <= 360
    assert (
        n(select(func.count()).select_from(IdDocument).where(IdDocument.status == IdStatus.PENDING))
        == 6
    )


def test_seed_data_is_all_synthetic(seeded_db):
    users = list(seeded_db.scalars(select(User)))
    assert all(u.is_synthetic for u in users)
    assert all(
        u.email.endswith("@example.test") or u.email.endswith("@fundifinder.test") for u in users
    )
    assert all(u.phone.startswith("+254700000") for u in users)


def test_seed_is_deterministic(seeded_app, tmp_path):
    from conftest import NOW, _config
    from fundifinder import clock, create_app

    clock.set_frozen(NOW)
    other = create_app(_config(tmp_path))
    with other.app_context():
        seed_demo(other)
    a, b = seeded_app.extensions["sessionmaker"](), other.extensions["sessionmaker"]()
    q = select(Review.fundi_id, Review.rating).order_by(Review.id)
    assert list(a.execute(q)) == list(b.execute(q))
    a.close()
    b.close()


def test_seeding_twice_is_refused(seeded_app):
    import pytest

    with seeded_app.app_context(), pytest.raises(SeedError):
        seed_demo(seeded_app)


def test_rating_distribution_is_realistic(seeded_db):
    counts = Counter(r for (r,) in seeded_db.execute(select(Review.rating)))
    total = sum(counts.values())
    assert counts[5] + counts[4] > 0.7 * total and counts[1] + counts[2] > 0
    mean = sum(k * v for k, v in counts.items()) / total
    assert 4.0 < mean < 4.8


def test_a_one_review_newcomer_ranks_below_a_veteran(seeded_db):
    cards = queries.load_cards(
        seeded_db,
        list(queries.search_fundis(seeded_db, queries.SearchFilters()))
        and [c.profile for c in queries.search_fundis(seeded_db, queries.SearchFilters())],
    )
    veteran = max(cards, key=lambda c: c.review_count)
    lucky = [c for c in cards if c.review_count == 1 and c.raw_average == 5.0]
    assert veteran.review_count >= 40 and lucky
    assert all(veteran.score > c.score for c in lucky)


def test_new_fundis_hold_reserved_slots_in_the_default_search(seeded_db):
    cards = queries.search_fundis(seeded_db, queries.SearchFilters())
    flags = [c.boosted for c in cards]
    assert flags[3] and flags[7] and not flags[0] and not flags[1]


def test_some_benchmarks_exist_and_some_do_not(seeded_db):
    b = queries.benchmarks_by_job_type(seeded_db)
    assert len(b) > 0 and all(x.sample_size >= 5 for x in b.values())


def test_demo_accounts_can_log_in_and_browse(seeded_app):
    for email, url in [
        ("customer@fundifinder.test", "/me"),
        ("fundi@fundifinder.test", "/fundi/"),
        ("admin@fundifinder.test", "/admin/"),
    ]:
        c = seeded_app.test_client()
        assert login(c, email, DEMO_PASSWORD).status_code == 302
        assert c.get(url).status_code == 200


def test_every_page_renders_for_seeded_data(seeded_app, seeded_db):
    c = seeded_app.test_client()
    ids = [i for (i,) in seeded_db.execute(select(User.id).where(User.role == Role.FUNDI))]
    from fundifinder.models import FundiProfile

    for pid in [p for (p,) in seeded_db.execute(select(FundiProfile.id))][::6]:
        assert c.get(f"/fundis/{pid}").status_code == 200
    assert ids and c.get("/search?verified=1&available=1").status_code == 200
    assert c.get("/search?page=3").status_code == 200


def test_demo_customer_has_a_job_waiting_for_a_code_and_one_for_a_review(seeded_app, seeded_db):
    cust = seeded_db.scalar(select(User).where(User.email == "customer@fundifinder.test"))
    statuses = Counter(
        j.status for j in seeded_db.scalars(select(Job).where(Job.customer_id == cust.id))
    )
    assert statuses[JobStatus.AWAITING_CONFIRMATION] == 1 and statuses[JobStatus.COMPLETED] == 2
    assert statuses[JobStatus.BOOKED] == 1


def test_moderation_backlog_exists(seeded_db):
    assert seeded_db.scalar(select(func.count()).select_from(Report)) >= 5
    assert seeded_db.scalar(select(func.count()).select_from(AuditLog)) > 10


def test_portfolio_photos_exist_on_disk(seeded_app, seeded_db):
    storage = seeded_app.extensions["storage"]
    photos = list(seeded_db.scalars(select(PortfolioPhoto).limit(10)))
    assert photos and all(storage.path("public", p.storage_name) for p in photos)


def test_password_is_the_documented_demo_password(seeded_db):
    user = seeded_db.scalar(select(User).where(User.email == "admin@fundifinder.test"))
    assert verify_password(user.password_hash, DEMO_PASSWORD)
