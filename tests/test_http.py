from __future__ import annotations

import io
import re

import pytest

from factories import (
    PASSWORD,
    code_for,
    make_admin,
    make_catalog,
    make_fundi,
    make_job,
    make_review,
    make_user,
)
from fundifinder.models import (
    IdDocument,
    IdStatus,
    JobStatus,
    PortfolioPhoto,
    QuoteRequest,
    Review,
    User,
)
from helpers import csrf, login, post
from imgs import image_bytes


@pytest.fixture
def world(app, db):
    cat = make_catalog(db)
    customer = make_user(db, 1)
    fundi = make_fundi(db, cat, 2)
    admin = make_admin(db)
    db.commit()
    return app, db, cat, customer, fundi, admin


def logged_in(app, user):
    c = app.test_client()
    assert login(c, user.email).status_code == 302
    return c


# ---- public pages -----------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/", "/search", "/trust", "/healthz", "/login", "/signup"])
def test_public_pages_render(client, url):
    assert client.get(url).status_code == 200


def test_profile_and_search_show_the_fundi(world):
    app, _, cat, _, fundi, _ = world
    c = app.test_client()
    assert fundi.user.name.encode() in c.get(f"/fundis/{fundi.id}").data
    assert fundi.user.name.encode() in c.get("/search?trade=plumber&area=kilimani").data
    assert b"No fundis match" in c.get("/search?area=westlands").data


def test_unknown_profile_is_404(client):
    assert client.get("/fundis/999").status_code == 404


def test_search_htmx_returns_a_fragment(world):
    app, *_ = world
    full = app.test_client().get("/search").data
    frag = app.test_client().get("/search", headers={"HX-Request": "true"}).data
    assert b"<html" in full and b"<html" not in frag and len(frag) < len(full)


def test_search_ignores_junk_filters(client):
    assert client.get("/search?min_price=abc&max_price=-4&trade=zzz&page=-3").status_code == 200


def test_error_pages_do_not_leak_details(client):
    r = client.get("/nope")
    assert r.status_code == 404 and b"Traceback" not in r.data


def test_security_headers_present(client):
    h = client.get("/").headers
    assert "default-src 'self'" in h["Content-Security-Policy"]
    assert (
        "script-src 'self'" in h["Content-Security-Policy"]
        and "unsafe-inline" not in h["Content-Security-Policy"]
    )
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"


def test_no_external_resources_in_pages(world):
    app, _, _, _, fundi, _ = world
    html = app.test_client().get(f"/fundis/{fundi.id}").get_data(as_text=True)
    assert not re.findall(r'(?:src|href)="https?://(?!wa\.me)', html)
    assert "htmx.min.js" in html


def test_whatsapp_and_tel_links(world):
    app, _, _, _, fundi, _ = world
    html = app.test_client().get(f"/fundis/{fundi.id}").get_data(as_text=True)
    assert "https://wa.me/" in html and 'href="tel:+254' in html


# ---- CSRF -------------------------------------------------------------------------------


def test_post_without_csrf_token_is_rejected(world):
    app, _, _, customer, _, _ = world
    c = app.test_client()
    c.get("/login")
    r = c.post("/login", data={"email": customer.email, "password": PASSWORD})
    assert r.status_code == 400


def test_post_with_wrong_csrf_token_is_rejected(world):
    app, _, _, customer, _, _ = world
    c = app.test_client()
    c.get("/login")
    r = c.post(
        "/login", data={"email": customer.email, "password": PASSWORD, "csrf_token": "x" * 43}
    )
    assert r.status_code == 400


def test_csrf_header_works_for_htmx(world):
    app, _, _, customer, _, _ = world
    c = logged_in(app, customer)
    token = csrf(c)
    r = c.post("/verify-phone/send", headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 200


def test_every_state_changing_route_rejects_a_missing_token(world):
    app, *_ = world
    c = app.test_client()
    for url in [
        "/logout",
        "/signup",
        "/requests/new",
        "/me/jobs/1/confirm",
        "/fundi/profile",
        "/admin/abuse/scan",
        "/reviews/1/report",
        "/fundi/jobs/1/code",
    ]:
        assert c.post(url, data={}).status_code == 400, url


def test_logout_is_post_only(world):
    app, _, _, customer, _, _ = world
    c = logged_in(app, customer)
    assert c.get("/logout").status_code == 405


# ---- auth -------------------------------------------------------------------------------


def test_signup_creates_account_and_logs_in(client, db):
    r = post(
        client,
        "/signup",
        {
            "role": "customer",
            "name": "Neema Test",
            "email": "Neema@Example.test",
            "phone": "0700 000 555",
            "password": "long-enough-pw",
        },
    )
    assert r.status_code == 302 and "/verify-phone" in r.location
    user = db.query(User).filter_by(email="neema@example.test").one()
    assert user.phone == "+254700000555" and user.password_hash != "long-enough-pw"
    assert client.get("/me").status_code == 200


def test_fundi_signup_creates_a_profile(client, db):
    cat = make_catalog(db)
    db.commit()
    post(
        client,
        "/signup",
        {
            "role": "fundi",
            "name": "Fundi Test",
            "email": "f@example.test",
            "phone": "0700000556",
            "password": "long-enough-pw",
            "trade": "plumber",
        },
    )
    assert (
        db.query(User).filter_by(email="f@example.test").one().fundi_profile.trade_id
        == cat.trade.id
    )


@pytest.mark.parametrize(
    "override",
    [{"email": "bad"}, {"phone": "123"}, {"password": "short"}, {"name": ""}, {"role": "admin"}],
)
def test_signup_validation(client, override):
    data = {
        "role": "customer",
        "name": "Neema Test",
        "email": "n@example.test",
        "phone": "0700000557",
        "password": "long-enough-pw",
        **override,
    }
    r = post(client, "/signup", data)
    assert r.status_code == 200 and b"error" in r.data


def test_signup_blocks_duplicate_email_variants_and_phone(world):
    app, _, _, customer, _, _ = world
    c = app.test_client()
    base = {
        "role": "customer",
        "name": "Dup Test",
        "phone": "0700000558",
        "password": "long-enough-pw",
    }
    r = post(c, "/signup", {**base, "email": customer.email.upper()})
    assert b"already exists" in r.data
    r = post(c, "/signup", {**base, "email": "other@example.test", "phone": customer.phone})
    assert b"phone number already exists" in r.data


def test_cannot_sign_up_as_admin_via_form(client, db):
    post(
        client,
        "/signup",
        {
            "role": "admin",
            "name": "Evil Admin",
            "email": "e@example.test",
            "phone": "0700000559",
            "password": "long-enough-pw",
        },
    )
    assert db.query(User).filter_by(email="e@example.test").first() is None


def test_login_wrong_password_and_unknown_user_look_the_same(world):
    app, _, _, customer, _, _ = world
    a = login(app.test_client(), customer.email, "nope-nope-nope")
    b = login(app.test_client(), "ghost@example.test", "nope-nope-nope")
    assert a.status_code == b.status_code == 200
    assert re.findall(r"incorrect", a.get_data(as_text=True)) == re.findall(
        r"incorrect", b.get_data(as_text=True)
    )


def test_login_next_only_follows_local_paths(world):
    app, _, _, customer, _, _ = world
    c = app.test_client()
    r = post(c, "/login?next=https://evil.example", {"email": customer.email, "password": PASSWORD})
    assert r.location.endswith("/me")
    c2 = app.test_client()
    r = post(c2, "/login?next=/search", {"email": customer.email, "password": PASSWORD})
    assert r.location.endswith("/search")


def test_login_rotates_the_session(world):
    app, _, _, customer, _, _ = world
    c = app.test_client()
    before = csrf(c)
    login(c, customer.email)
    assert csrf(c) != before


def test_suspended_users_cannot_log_in_or_keep_a_session(world):
    app, db, _, customer, _, _ = world
    c = logged_in(app, customer)
    customer.is_suspended = True
    db.commit()
    assert c.get("/me").status_code == 302
    assert b"suspended" in login(app.test_client(), customer.email).data


def test_login_rate_limit(tmp_path):
    from conftest import _config
    from fundifinder import create_app

    app = create_app({**_config(tmp_path), "RATELIMIT_ENABLED": True})
    c = app.test_client()
    codes = [login(c, "x@example.test", "bad-bad-bad").status_code for _ in range(10)]
    assert codes[:8] == [200] * 8 and codes[8] == 429


# ---- role and ownership permissions -----------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/me", "/fundi/", "/admin/", "/requests/new?fundi=1", "/me/notices"]
)
def test_anonymous_users_are_sent_to_login(client, url):
    r = client.get(url)
    assert r.status_code == 302 and "/login" in r.location


def test_roles_are_enforced(world):
    app, _, _, customer, fundi, admin = world
    cust, fund, adm = logged_in(app, customer), logged_in(app, fundi.user), logged_in(app, admin)
    assert cust.get("/fundi/").status_code == 403 and cust.get("/admin/").status_code == 403
    assert fund.get("/me").status_code == 403 and fund.get("/admin/").status_code == 403
    assert adm.get("/admin/").status_code == 200 and adm.get("/fundi/").status_code == 403


def test_customers_cannot_see_other_customers_jobs(world):
    app, db, cat, customer, fundi, _ = world
    stranger = make_user(db, 8)
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    db.commit()
    assert logged_in(app, customer).get(f"/me/jobs/{job.id}").status_code == 200
    assert logged_in(app, stranger).get(f"/me/jobs/{job.id}").status_code == 404


def test_fundi_never_sees_the_completion_code(world):
    app, db, cat, customer, fundi, _ = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    db.commit()
    code = code_for(job)
    assert code in logged_in(app, customer).get(f"/me/jobs/{job.id}").get_data(
        as_text=True
    ).replace(" ", "")
    fundi_page = logged_in(app, fundi.user).get(f"/fundi/jobs/{job.id}").get_data(as_text=True)
    assert code not in fundi_page.replace(" ", "")


def test_other_fundis_cannot_open_a_job_or_request(world):
    app, db, cat, customer, fundi, _ = world
    other = make_fundi(db, cat, 9)
    job = make_job(db, customer, fundi, cat)
    db.commit()
    c = logged_in(app, other.user)
    assert c.get(f"/fundi/jobs/{job.id}").status_code == 404
    assert post(c, f"/fundi/jobs/{job.id}/start").status_code == 404
    assert c.get(f"/fundi/requests/{job.request_id}").status_code == 404


def test_fundi_cannot_review_their_own_job_over_http(world):
    app, db, cat, customer, fundi, _ = world
    job = make_job(db, customer, fundi, cat, JobStatus.COMPLETED)
    db.commit()
    c = logged_in(app, fundi.user)
    assert post(c, f"/me/jobs/{job.id}/review", {"rating": "5"}).status_code == 403
    assert db.query(Review).count() == 0


def test_review_over_http_requires_the_code(world):
    app, db, cat, customer, fundi, _ = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    db.commit()
    c = logged_in(app, customer)
    post(c, f"/me/jobs/{job.id}/review", {"rating": "5", "body": "great"})
    assert db.query(Review).count() == 0
    post(c, f"/me/jobs/{job.id}/confirm", {"code": code_for(job)})
    post(c, f"/me/jobs/{job.id}/review", {"rating": "5", "body": "great"})
    assert db.query(Review).count() == 1


def test_wrong_code_lockout_over_http(world):
    app, db, cat, customer, fundi, _ = world
    job = make_job(db, customer, fundi, cat, JobStatus.AWAITING_CONFIRMATION)
    db.commit()
    c = logged_in(app, customer)
    wrong = "000000" if code_for(job) != "000000" else "111111"
    for _ in range(5):
        post(c, f"/me/jobs/{job.id}/confirm", {"code": wrong})
    r = post(c, f"/me/jobs/{job.id}/confirm", {"code": code_for(job)}, follow_redirects=True)
    assert b"locked" in r.data
    db.expire_all()
    assert db.get(type(job), job.id).status == JobStatus.AWAITING_CONFIRMATION


def test_admin_pages_are_closed_to_everyone_else(world):
    app, _, _, customer, fundi, _ = world
    for who in (app.test_client(), logged_in(app, customer), logged_in(app, fundi.user)):
        for url in [
            "/admin/verification",
            "/admin/audit",
            "/admin/users",
            "/admin/documents/1/file",
        ]:
            assert who.get(url).status_code in (302, 403)


# ---- ID documents stay private ----------------------------------------------------------


def upload_id(app, db, fundi):
    c = logged_in(app, fundi.user)
    r = post(
        c,
        "/fundi/verification/id",
        {"document": (io.BytesIO(image_bytes()), "id.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    return db.query(IdDocument).one()


def test_id_document_is_never_public(world):
    app, db, _, customer, fundi, admin = world
    doc = upload_id(app, db, fundi)
    anon, cust, own = app.test_client(), logged_in(app, customer), logged_in(app, fundi.user)
    url = f"/admin/documents/{doc.id}/file"
    for who in (anon, cust, own):
        assert who.get(url).status_code in (302, 403)
    for guess in (
        f"/media/portfolio/{doc.storage_name}",
        f"/static/{doc.storage_name}",
        f"/uploads/{doc.storage_name}",
        f"/static/../uploads/private/{doc.storage_name}",
    ):
        assert anon.get(guess).status_code == 404


def test_admin_can_view_the_document_uncached_and_it_is_logged(world):
    app, db, _, _, fundi, admin = world
    doc = upload_id(app, db, fundi)
    r = logged_in(app, admin).get(f"/admin/documents/{doc.id}/file")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert "no-store" in r.headers["Cache-Control"]
    assert b"id.viewed" in logged_in(app, admin).get("/admin/audit").data


def test_fundi_profile_never_links_the_document(world):
    app, db, _, _, fundi, _ = world
    doc = upload_id(app, db, fundi)
    html = app.test_client().get(f"/fundis/{fundi.id}").get_data(as_text=True)
    assert doc.storage_name not in html and "/admin/documents" not in html


def test_approval_flow_over_http_adds_the_badge(world):
    app, db, _, _, fundi, admin = world
    doc = upload_id(app, db, fundi)
    adm = logged_in(app, admin)
    assert (
        post(adm, f"/admin/verification/{doc.id}/decide", {"decision": "approve"}).status_code
        == 302
    )
    db.expire_all()
    assert db.get(IdDocument, doc.id).status == IdStatus.APPROVED
    assert b"ID checked" in app.test_client().get(f"/fundis/{fundi.id}").data


def test_id_upload_rejects_non_images_over_http(world):
    app, db, _, _, fundi, _ = world
    c = logged_in(app, fundi.user)
    post(
        c,
        "/fundi/verification/id",
        {"document": (io.BytesIO(b"<svg onload=alert(1)>"), "id.png")},
        content_type="multipart/form-data",
    )
    assert db.query(IdDocument).count() == 0


# ---- uploads over HTTP ------------------------------------------------------------------


def test_request_photo_is_visible_to_parties_only(world):
    app, db, cat, customer, fundi, admin = world
    stranger = make_user(db, 8)
    db.commit()
    c = logged_in(app, customer)
    r = post(
        c,
        "/requests/new",
        {
            "fundi": fundi.id,
            "job_type": cat.job_type.id,
            "area": cat.area.id,
            "description": "The tap in the kitchen leaks all night",
            "preferred_time": "2026-06-20T09:00",
            "photo": (io.BytesIO(image_bytes("JPEG", exif=True)), "tap.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    req = db.query(QuoteRequest).one()
    assert req.photo_name
    url = f"/uploads/request/{req.id}"
    assert c.get(url).status_code == 200
    assert logged_in(app, fundi.user).get(url).status_code == 200
    assert logged_in(app, admin).get(url).status_code == 200
    assert logged_in(app, stranger).get(url).status_code == 404
    assert app.test_client().get(url).status_code == 302
    body = c.get(url).data
    assert b"SecretCameraMaker" not in body


def test_request_with_bad_photo_is_rejected_and_nothing_saved(world):
    app, db, cat, customer, fundi, _ = world
    c = logged_in(app, customer)
    post(
        c,
        "/requests/new",
        {
            "fundi": fundi.id,
            "job_type": cat.job_type.id,
            "area": cat.area.id,
            "description": "The tap in the kitchen leaks all night",
            "preferred_time": "2026-06-20T09:00",
            "photo": (io.BytesIO(b"MZ not an image"), "tap.png"),
        },
        content_type="multipart/form-data",
    )
    assert db.query(QuoteRequest).count() == 0


def test_oversize_upload_is_refused(world):
    app, db, *_ = world
    app.config["MAX_CONTENT_LENGTH"] = 1000
    _, _, cat, customer, fundi, _ = world
    c = logged_in(app, customer)
    r = post(
        c,
        "/requests/new",
        {"fundi": fundi.id, "photo": (io.BytesIO(b"x" * 5000), "a.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 413


def test_portfolio_upload_is_public_and_earns_the_badge_at_three(world):
    app, db, _, _, fundi, _ = world
    c = logged_in(app, fundi.user)
    for i in range(3):
        r = post(
            c,
            "/fundi/portfolio",
            {"photo": (io.BytesIO(image_bytes()), f"w{i}.png"), "caption": f"Job {i}"},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert r.status_code == 200
    photos = db.query(PortfolioPhoto).all()
    assert len(photos) == 3
    anon = app.test_client()
    assert anon.get(f"/media/portfolio/{photos[0].storage_name}").status_code == 200
    assert b"Trade skills" in anon.get(f"/fundis/{fundi.id}").data


def test_media_route_rejects_traversal(client):
    for bad in ("..%2f..%2fetc%2fpasswd", "x.png", "../x"):
        assert client.get(f"/media/portfolio/{bad}").status_code == 404


# ---- phone verification flow ------------------------------------------------------------


def test_phone_verification_over_http(app, db):
    c = app.test_client()
    post(
        c,
        "/signup",
        {
            "role": "customer",
            "name": "Phone Test",
            "email": "p@example.test",
            "phone": "0700000601",
            "password": "long-enough-pw",
        },
    )
    post(c, "/verify-phone/send")
    sms = app.extensions["sms"]
    code = sms.last_for("+254700000601").body.split("code is ")[1][:6]
    assert (
        post(
            c, "/verify-phone/check", {"code": "000000" if code != "000000" else "111111"}
        ).status_code
        == 400
    )
    assert post(c, "/verify-phone/check", {"code": code}).status_code == 302
    db.expire_all()
    assert db.query(User).filter_by(email="p@example.test").one().phone_verified_at is not None


def test_dev_outbox_page_only_in_dev(app, tmp_path):
    from conftest import _config
    from fundifinder import create_app

    assert app.test_client().get("/dev/outbox").status_code == 200
    off = create_app({**_config(tmp_path), "DEV_OUTBOX": False})
    assert off.test_client().get("/dev/outbox").status_code == 404


# ---- reports and moderation over HTTP ---------------------------------------------------


def test_report_and_resolve_over_http(world):
    app, db, cat, customer, fundi, admin = world
    reporter = make_user(db, 8)
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED), 1, "awful")
    db.commit()
    rc = logged_in(app, reporter)
    assert (
        post(
            rc, f"/reviews/{review.id}/report", {"reason": "abusive", "target": "review"}
        ).status_code
        == 302
    )
    adm = logged_in(app, admin)
    assert b"awful" in adm.get("/admin/reports").data
    from fundifinder.models import Report

    report = db.query(Report).one()
    post(adm, f"/admin/reports/{report.id}/resolve", {"decision": "uphold", "note": "rude"})
    assert b"awful" not in app.test_client().get(f"/fundis/{fundi.id}").data
    notices = logged_in(app, customer).get("/me/notices").get_data(as_text=True)
    assert "Submit appeal" in notices


def test_anonymous_cannot_report(world):
    app, db, cat, customer, fundi, _ = world
    review = make_review(db, make_job(db, customer, fundi, cat, JobStatus.COMPLETED))
    db.commit()
    c = app.test_client()
    assert post(c, f"/reviews/{review.id}/report", {"reason": "fake"}).status_code == 302


def test_user_suspension_by_admin(world):
    app, db, _, customer, _, admin = world
    adm = logged_in(app, admin)
    post(adm, f"/admin/users/{customer.id}/suspend", {"action": "suspend", "reason": "test"})
    db.expire_all()
    assert db.get(User, customer.id).is_suspended
    post(adm, f"/admin/users/{admin.id}/suspend", {"action": "suspend"})
    assert not db.get(User, admin.id).is_suspended


def test_output_is_escaped(world):
    app, db, cat, customer, fundi, _ = world
    fundi.headline = "<script>alert(1)</script>"
    db.commit()
    html = app.test_client().get(f"/fundis/{fundi.id}").get_data(as_text=True)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
