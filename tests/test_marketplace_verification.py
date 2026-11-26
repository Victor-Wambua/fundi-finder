from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import NOW, SECRET
from factories import make_admin, make_catalog, make_fundi, make_job, make_user
from fundifinder import marketplace, queries, verification
from fundifinder.lifecycle import Event
from fundifinder.marketplace import RuleViolation
from fundifinder.models import (
    AuditLog,
    IdStatus,
    JobStatus,
    QuoteStatus,
    RefStatus,
    RequestStatus,
)
from fundifinder.sms import ConsoleSms
from fundifinder.uploads import Storage, UploadError
from imgs import image_bytes


@pytest.fixture
def world(db):
    cat = make_catalog(db)
    return db, cat, make_user(db, 1), make_fundi(db, cat, 2)


def request_for(db, cat, customer, fundi, when=None):
    return marketplace.create_request(
        db,
        customer,
        fundi,
        cat.job_type.id,
        cat.area.id,
        "Tap is leaking badly",
        when or NOW + timedelta(days=2),
    )


# ---- request -> quote -> booking ---------------------------------------------------------


def test_request_quote_accept_creates_a_booked_job_with_a_code_salt(world):
    db, cat, customer, fundi = world
    req = request_for(db, cat, customer, fundi)
    quote = marketplace.send_quote(db, fundi.user, req, 2500, "ok", NOW + timedelta(days=2))
    assert req.status == RequestStatus.QUOTED and req.first_response_at == NOW
    job = marketplace.accept_quote(db, customer, quote)
    assert job.status == JobStatus.BOOKED and job.price_kes == 2500 and len(job.code_salt) == 32
    assert quote.status == QuoteStatus.ACCEPTED and req.status == RequestStatus.ACCEPTED


def test_unverified_customer_cannot_request(db):
    cat = make_catalog(db)
    with pytest.raises(RuleViolation, match="Verify your phone"):
        request_for(db, cat, make_user(db, 1, verified=False), make_fundi(db, cat, 2))


def test_unverified_fundi_cannot_quote(db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2, verified=False)
    req = request_for(db, cat, make_user(db, 1), fundi)
    with pytest.raises(RuleViolation, match="Verify your phone"):
        marketplace.send_quote(db, fundi.user, req, 2000, "", NOW + timedelta(days=1))


def test_request_must_match_fundis_services_and_areas(world):
    db, cat, customer, fundi = world
    with pytest.raises(RuleViolation, match="area"):
        marketplace.create_request(
            db, customer, fundi, cat.job_type.id, cat.area2.id, "x" * 20, NOW + timedelta(days=1)
        )
    with pytest.raises(RuleViolation, match="does not offer"):
        marketplace.create_request(
            db, customer, fundi, 9999, cat.area.id, "x" * 20, NOW + timedelta(days=1)
        )


def test_request_in_the_past_refused(world):
    db, cat, customer, fundi = world
    with pytest.raises(RuleViolation, match="future"):
        request_for(db, cat, customer, fundi, NOW - timedelta(days=1))


def test_fundis_cannot_request_quotes(world):
    db, cat, _, fundi = world
    other = make_fundi(db, cat, 5)
    with pytest.raises(RuleViolation, match="Only customers"):
        request_for(db, cat, other.user, fundi)


def test_open_request_cap(world):
    db, cat, customer, fundi = world
    for _ in range(marketplace.MAX_OPEN_REQUESTS_PER_CUSTOMER):
        request_for(db, cat, customer, fundi)
    with pytest.raises(RuleViolation, match="too many"):
        request_for(db, cat, customer, fundi)


def test_quote_bounds_and_ownership(world):
    db, cat, customer, fundi = world
    other = make_fundi(db, cat, 5)
    req = request_for(db, cat, customer, fundi)
    for amount in (99, 5_000_001):
        with pytest.raises(RuleViolation):
            marketplace.send_quote(db, fundi.user, req, amount, "", NOW + timedelta(days=1))
    with pytest.raises(RuleViolation, match="not sent to you"):
        marketplace.send_quote(db, other.user, req, 2000, "", NOW + timedelta(days=1))
    with pytest.raises(RuleViolation, match="future"):
        marketplace.send_quote(db, fundi.user, req, 2000, "", NOW - timedelta(hours=1))


def test_new_quote_supersedes_the_old_one(world):
    db, cat, customer, fundi = world
    req = request_for(db, cat, customer, fundi)
    first = marketplace.send_quote(db, fundi.user, req, 3000, "", NOW + timedelta(days=1))
    second = marketplace.send_quote(db, fundi.user, req, 2500, "", NOW + timedelta(days=1))
    assert first.status == QuoteStatus.SUPERSEDED and second.status == QuoteStatus.SENT
    with pytest.raises(RuleViolation):
        marketplace.accept_quote(db, customer, first)


def test_only_the_requesting_customer_accepts_and_only_once(world):
    db, cat, customer, fundi = world
    stranger = make_user(db, 9)
    req = request_for(db, cat, customer, fundi)
    quote = marketplace.send_quote(db, fundi.user, req, 2500, "", NOW + timedelta(days=1))
    with pytest.raises(RuleViolation):
        marketplace.accept_quote(db, stranger, quote)
    marketplace.accept_quote(db, customer, quote)
    with pytest.raises(RuleViolation):
        marketplace.accept_quote(db, customer, quote)


def test_decline_and_cancel(world):
    db, cat, customer, fundi = world
    req = request_for(db, cat, customer, fundi)
    marketplace.decline_request(db, fundi.user, req)
    assert req.status == RequestStatus.DECLINED and req.first_response_at is not None
    with pytest.raises(RuleViolation):
        marketplace.cancel_request(db, customer, req)
    req2 = request_for(db, cat, customer, fundi)
    marketplace.cancel_request(db, customer, req2)
    assert req2.status == RequestStatus.CANCELLED


def test_apply_event_checks_who_you_are(world):
    db, cat, customer, fundi = world
    job = make_job(db, customer, fundi, cat)
    stranger = make_user(db, 9)
    with pytest.raises(RuleViolation, match="not part"):
        marketplace.apply_event(db, stranger, job, Event.CANCEL)
    with pytest.raises(RuleViolation):
        marketplace.apply_event(db, customer, job, Event.START)
    marketplace.apply_event(db, fundi.user, job, Event.START)
    assert job.status == JobStatus.IN_PROGRESS


def test_admin_cancel_is_audited(world):
    db, cat, customer, fundi = world
    job = make_job(db, customer, fundi, cat)
    marketplace.apply_event(db, make_admin(db), job, Event.CANCEL, "dispute")
    assert db.scalar(select(AuditLog).where(AuditLog.action == "job.cancel")) is not None


# ---- ranking inputs from real rows ------------------------------------------------------


def test_response_rate_and_punctuality_flow_into_stats(world):
    db, cat, customer, fundi = world
    for hours in (1, 2, 30):  # two quick replies, one slow
        req = make_job(db, customer, fundi, cat, JobStatus.COMPLETED).request
        req.first_response_at = req.created_at + timedelta(hours=hours)
    card = queries.load_cards(db, [fundi])[0]
    assert card.stats.requests_answerable == 3 and card.stats.requests_answered_in_time == 2
    assert card.stats.punctual_eligible == 3 and card.stats.punctual_jobs == 3
    assert card.stats.completed_jobs == 3


def test_unanswered_old_request_counts_against_response_rate(world):
    db, cat, customer, fundi = world
    req = request_for(db, cat, customer, fundi)
    req.created_at = NOW - timedelta(days=3)
    assert queries.load_cards(db, [fundi])[0].stats.requests_answerable == 1
    req.created_at = NOW - timedelta(hours=3)
    assert queries.load_cards(db, [fundi])[0].stats.requests_answerable == 0


def test_price_benchmark_needs_five_completed_jobs(world):
    db, cat, customer, fundi = world
    for price in (1000, 2000, 3000, 4000):
        make_job(db, customer, fundi, cat, JobStatus.COMPLETED, price=price)
    assert queries.job_type_benchmark(db, cat.job_type.id) is None
    make_job(db, customer, fundi, cat, JobStatus.COMPLETED, price=5000)
    make_job(
        db, customer, fundi, cat, JobStatus.CANCELLED, price=999999
    )  # cancelled jobs never count
    b = queries.job_type_benchmark(db, cat.job_type.id)
    assert b is not None and b.median_kes == 3000 and b.sample_size == 5


def test_search_filters(world):
    db, cat, customer, fundi = world
    assert len(queries.search_fundis(db, queries.SearchFilters(trade_id=cat.trade.id))) == 1
    assert queries.search_fundis(db, queries.SearchFilters(area_id=cat.area2.id)) == []
    assert queries.search_fundis(db, queries.SearchFilters(min_price=5000)) == []
    assert queries.search_fundis(db, queries.SearchFilters(max_price=500)) == []
    assert (
        len(queries.search_fundis(db, queries.SearchFilters(min_price=2500, max_price=4000))) == 1
    )
    assert queries.search_fundis(db, queries.SearchFilters(verified_only=True)) == []


def test_suspended_fundis_disappear_from_search(world):
    db, cat, _, fundi = world
    fundi.user.is_suspended = True
    assert queries.search_fundis(db, queries.SearchFilters()) == []


def test_newcomers_with_verified_phone_get_reserved_slots(db):
    cat = make_catalog(db)
    for n in range(2, 12):
        make_fundi(db, cat, n)
    cards = queries.search_fundis(db, queries.SearchFilters())
    assert all(c.boosted for c in cards)  # nobody has jobs yet: all are new
    unverified = make_fundi(db, cat, 20, verified=False)
    cards = queries.search_fundis(db, queries.SearchFilters())
    assert not next(c for c in cards if c.profile.id == unverified.id).boosted


# ---- phone OTP --------------------------------------------------------------------------


def otp_code(sms: ConsoleSms, phone: str) -> str:
    return sms.last_for(phone).body.split("code is ")[1][:6]  # type: ignore[union-attr]


def test_otp_round_trip(db):
    user = make_user(db, 1, verified=False)
    sms = ConsoleSms()
    verification.send_otp(db, user, sms, SECRET)
    assert verification.check_otp(db, user, otp_code(sms, user.phone), SECRET)
    assert user.phone_verified_at == NOW


def test_wrong_otp_then_five_attempts_lock_the_code(db):
    user = make_user(db, 1, verified=False)
    sms = ConsoleSms()
    verification.send_otp(db, user, sms, SECRET)
    real = otp_code(sms, user.phone)
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(5):
        assert verification.check_otp(db, user, wrong, SECRET) is False
    with pytest.raises(RuleViolation, match="Too many wrong"):
        verification.check_otp(db, user, real, SECRET)


def test_otp_expires_after_ten_minutes(db):
    from fundifinder import clock

    user = make_user(db, 1, verified=False)
    sms = ConsoleSms()
    verification.send_otp(db, user, sms, SECRET)
    with clock.frozen(NOW + timedelta(minutes=11)), pytest.raises(RuleViolation, match="expired"):
        verification.check_otp(db, user, otp_code(sms, user.phone), SECRET)


def test_new_code_invalidates_the_old_one_and_sends_are_capped(db):
    user = make_user(db, 1, verified=False)
    sms = ConsoleSms()
    verification.send_otp(db, user, sms, SECRET)
    first = otp_code(sms, user.phone)
    verification.send_otp(db, user, sms, SECRET)
    second = otp_code(sms, user.phone)
    if first != second:
        assert verification.check_otp(db, user, first, SECRET) is False
    verification.send_otp(db, user, sms, SECRET)
    with pytest.raises(RuleViolation, match="Too many codes"):
        verification.send_otp(db, user, sms, SECRET)


def test_otp_is_not_stored_in_clear(db):
    from fundifinder.models import PhoneOtp

    user = make_user(db, 1, verified=False)
    sms = ConsoleSms()
    verification.send_otp(db, user, sms, SECRET)
    row = db.scalar(select(PhoneOtp))
    assert otp_code(sms, user.phone) not in row.code_hash  # type: ignore[union-attr]


# ---- ID documents and references --------------------------------------------------------


def test_id_document_review_flow_and_badge(db, tmp_path):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    admin = make_admin(db)
    storage = Storage(tmp_path)
    doc = verification.submit_id_document(db, fundi, image_bytes(), "id.png", storage, 10**6)
    assert doc.status == IdStatus.PENDING and storage.path("private", doc.storage_name)
    assert "id" not in {b.key.value for b in queries.load_cards(db, [fundi])[0].badges}
    with pytest.raises(RuleViolation, match="waiting"):
        verification.submit_id_document(db, fundi, image_bytes(), "id.png", storage, 10**6)
    with pytest.raises(RuleViolation, match="Only admins"):
        verification.review_id_document(db, fundi.user, doc, True, "")
    verification.review_id_document(db, admin, doc, True, "looks fine")
    assert "id" in {b.key.value for b in queries.load_cards(db, [fundi])[0].badges}
    assert db.scalar(select(AuditLog).where(AuditLog.action == "id.approved")) is not None


def test_rejection_needs_a_reason(db, tmp_path):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    doc = verification.submit_id_document(
        db, fundi, image_bytes(), "id.png", Storage(tmp_path), 10**6
    )
    with pytest.raises(RuleViolation, match="why"):
        verification.review_id_document(db, make_admin(db), doc, False, " ")


def test_bad_id_upload_is_rejected_before_storage(db, tmp_path):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    with pytest.raises(UploadError):
        verification.submit_id_document(db, fundi, b"<svg/>", "id.svg", Storage(tmp_path), 10**6)


def test_two_confirmed_references_earn_the_badge(db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    sms = ConsoleSms()
    refs = [
        verification.add_reference(db, fundi, f"Ref {i}", f"0700 000 10{i}", "", sms, "http://x")
        for i in range(2)
    ]
    assert len(sms.outbox) == 2 and refs[0].token in sms.outbox[1].body
    assert "references" not in {b.key.value for b in queries.load_cards(db, [fundi])[0].badges}
    for r in refs:
        verification.respond_to_reference(db, r, True)
    assert "references" in {b.key.value for b in queries.load_cards(db, [fundi])[0].badges}
    with pytest.raises(RuleViolation):
        verification.respond_to_reference(db, refs[0], False)


def test_declined_references_do_not_count(db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    sms = ConsoleSms()
    for i in range(3):
        r = verification.add_reference(
            db, fundi, f"Ref {i}", f"0700 000 20{i}", "", sms, "http://x"
        )
        verification.respond_to_reference(db, r, i == 0)
    assert r.status == RefStatus.DECLINED
    assert "references" not in {b.key.value for b in queries.load_cards(db, [fundi])[0].badges}


def test_reference_rules(db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    sms = ConsoleSms()
    with pytest.raises(RuleViolation, match="own number"):
        verification.add_reference(db, fundi, "Me", fundi.user.phone, "", sms, "http://x")
    with pytest.raises(RuleViolation, match="mobile"):
        verification.add_reference(db, fundi, "Bad", "12", "", sms, "http://x")
    verification.add_reference(db, fundi, "A", "0700 000 301", "", sms, "http://x")
    with pytest.raises(RuleViolation, match="already added"):
        verification.add_reference(db, fundi, "A again", "0700000301", "", sms, "http://x")
