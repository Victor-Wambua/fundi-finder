"""End to end through the HTTP layer: request -> quote -> booking -> complete -> review."""

from __future__ import annotations

import io
import re
from datetime import timedelta

from conftest import NOW
from factories import make_catalog, make_fundi, make_user
from fundifinder import clock, queries
from fundifinder.models import Job, JobStatus, QuoteRequest, Review
from helpers import login, post
from imgs import image_bytes


def verify_phone(app, client, phone: str) -> None:
    post(client, "/verify-phone/send")
    code = app.extensions["sms"].last_for(phone).body.split("code is ")[1][:6]
    assert post(client, "/verify-phone/check", {"code": code}).status_code == 302


def page_code(client, job_id: int) -> str:
    html = client.get(f"/me/jobs/{job_id}").get_data(as_text=True)
    return re.search(r'aria-label="Completion code (\d{6})"', html).group(1)  # type: ignore[union-attr]


def run_flow(app, db, confirm_as: str) -> tuple[Job, Review]:
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    db.commit()
    when = (NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    # customer signs up and verifies the phone
    cust = app.test_client()
    post(
        cust,
        "/signup",
        {
            "role": "customer",
            "name": "Neema Wanjiru",
            "email": "neema@example.test",
            "phone": "0700000777",
            "password": "long-enough-pw",
        },
    )
    verify_phone(app, cust, "+254700000777")

    # customer requests a quote, with a photo
    r = post(
        cust,
        "/requests/new",
        {
            "fundi": fundi.id,
            "job_type": cat.job_type.id,
            "area": cat.area.id,
            "description": "Kitchen tap leaks even when closed",
            "preferred_time": when,
            "photo": (io.BytesIO(image_bytes("JPEG")), "tap.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    req = db.query(QuoteRequest).one()

    # fundi sees it and quotes
    f = app.test_client()
    login(f, fundi.user.email)
    assert b"Kitchen tap leaks" in f.get(f"/fundi/requests/{req.id}").data
    r = post(
        f,
        f"/fundi/requests/{req.id}/quote",
        {"amount": "2,400", "start": when, "message": "Includes washer"},
    )
    assert r.status_code == 302

    # customer sees the quote and accepts: a booking with a code appears
    db.expire_all()
    quote = db.get(QuoteRequest, req.id).quotes[0]
    assert quote.amount_kes == 2400
    assert b"KES 2,400" in cust.get(f"/me/requests/{req.id}").data
    r = post(cust, f"/me/quotes/{quote.id}/accept")
    job = db.query(Job).one()
    assert r.status_code == 302 and job.status == JobStatus.BOOKED
    code = page_code(cust, job.id)

    # on site: fundi starts, finishes
    with clock.frozen(NOW + timedelta(days=2, minutes=5)):
        post(f, f"/fundi/jobs/{job.id}/start")
        post(f, f"/fundi/jobs/{job.id}/complete")
    db.expire_all()
    assert db.get(Job, job.id).status == JobStatus.AWAITING_CONFIRMATION

    # no review yet
    post(cust, f"/me/jobs/{job.id}/review", {"rating": "5", "body": "early"})
    assert db.query(Review).count() == 0

    # a wrong code does nothing; the right code (from either side) completes it
    post(cust, f"/me/jobs/{job.id}/confirm", {"code": "123456" if code != "123456" else "654321"})
    db.expire_all()
    assert db.get(Job, job.id).status == JobStatus.AWAITING_CONFIRMATION
    if confirm_as == "customer":
        post(cust, f"/me/jobs/{job.id}/confirm", {"code": code})
    else:
        post(f, f"/fundi/jobs/{job.id}/code", {"code": code})
    db.expire_all()
    done = db.get(Job, job.id)
    assert done.status == JobStatus.COMPLETED and done.code_confirmed_by == confirm_as

    # the code is single use
    post(cust, f"/me/jobs/{job.id}/confirm", {"code": code})
    assert db.get(Job, job.id).code_used_at == done.code_used_at

    # review, then the fundi replies publicly
    post(cust, f"/me/jobs/{job.id}/review", {"rating": "5", "body": "Fixed in twenty minutes."})
    review = db.query(Review).one()
    post(f, f"/fundi/reviews/{review.id}/reply", {"body": "Thank you, Neema."})
    return done, review


def test_full_flow_customer_confirms(app, db):
    job, review = run_flow(app, db, "customer")
    page = app.test_client().get(f"/fundis/{job.fundi_id}").get_data(as_text=True)
    assert "Fixed in twenty minutes." in page and "Thank you, Neema." in page
    assert "Confirmed job: Fix a leaking tap" in page
    assert "Neema W." in page and "Neema Wanjiru" not in page


def test_full_flow_fundi_enters_the_code_the_customer_read_out(app, db):
    job, _ = run_flow(app, db, "fundi")
    assert job.code_confirmed_by == "fundi"


def test_flow_moves_the_fundis_ranking_inputs(app, db):
    job, review = run_flow(app, db, "customer")
    db.expire_all()
    card = queries.load_cards(db, [job and db.get(type(job), job.id).fundi])[0]
    assert card.review_count == 1 and card.completed_jobs == 1
    assert card.stats.punctual_eligible == 1 and card.stats.requests_answered_in_time == 1


def test_a_second_review_for_the_same_job_is_impossible(app, db):
    job, _ = run_flow(app, db, "customer")
    c = app.test_client()
    login(c, "neema@example.test", "long-enough-pw")
    post(c, f"/me/jobs/{job.id}/review", {"rating": "1", "body": "changed my mind"})
    assert db.query(Review).count() == 1


def test_unverified_customer_cannot_request_over_http(app, db):
    cat = make_catalog(db)
    fundi = make_fundi(db, cat, 2)
    make_user(db, 5, verified=False)
    db.commit()
    c = app.test_client()
    login(c, "user5@example.test")
    post(
        c,
        "/requests/new",
        {
            "fundi": fundi.id,
            "job_type": cat.job_type.id,
            "area": cat.area.id,
            "description": "Kitchen tap leaks even when closed",
            "preferred_time": (NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert db.query(QuoteRequest).count() == 0
