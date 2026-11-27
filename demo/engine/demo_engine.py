"""Engine for the static browser demo.

This file is glue, not a second implementation. Every rule you see in the demo is decided by
the real ``fundifinder`` modules: ``marketplace`` (quote, booking, code confirmation),
``lifecycle`` (job state machine), ``completion`` (one-time codes), ``reviews`` (the review
gate), ``queries`` and ``ranking`` (search, scores, aggregates) and ``models`` (the SQLAlchemy
tables). The only things written here are the invented seed data, a JSON wrapper around those
modules, the demo clock and saving the database to browser storage.

It runs under Pyodide in the browser and under CPython in the test suite
(``tests/test_demo_engine.py``). It never imports Flask or Pillow.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from fundifinder import audit, clock, completion, marketplace, queries, ranking, reviews
from fundifinder.availability import WeeklyRule, summarise
from fundifinder.db import Base, make_engine
from fundifinder.lifecycle import TERMINAL, Actor, Event, allowed_events
from fundifinder.marketplace import RuleViolation
from fundifinder.models import (
    Area,
    AvailabilityException,
    AvailabilityRule,
    FundiProfile,
    FundiService,
    IdDocument,
    IdStatus,
    Job,
    JobStatus,
    JobType,
    PortfolioPhoto,
    Quote,
    QuoteRequest,
    QuoteStatus,
    Reference,
    RefStatus,
    RequestStatus,
    Review,
    ReviewStatus,
    Role,
    Trade,
    User,
)
from fundifinder.phones import display_phone

STATE_VERSION = 3

# In the real app this secret lives on the server and signs every code. Here it sits in your
# browser, so the demo shows the mechanism, not the secrecy.
DEMO_SECRET = "demo-secret-this-one-is-public"
CUSTOMER_EMAIL = "amina.wairimu@demo.test"
ADMIN_EMAIL = "admin@demo.test"

# Wording copied from the web layer (blueprints/customer.py) for each code result.
CODE_MESSAGES = {
    completion.CodeResult.OK: "Confirmed. The job is complete, and you can now leave a review.",
    completion.CodeResult.WRONG: "That code is not right. {left} attempt(s) left before a lockout.",
    completion.CodeResult.LOCKED: "Too many wrong codes. Entry is locked for an hour.",
    completion.CodeResult.INVALIDATED: (
        "This code has been disabled after too many wrong attempts. Support must re-issue it."
    ),
    completion.CodeResult.USED: "This code has already been used.",
}

AREAS = ["Kilimani", "Westlands", "Kileleshwa", "South B", "Embakasi", "Roysambu"]

# slug -> (name, plural, [(job type, min KES, max KES)])
TRADES: dict[str, tuple[str, str, list[tuple[str, int, int]]]] = {
    "plumber": (
        "Plumber",
        "Plumbers",
        [
            ("Fix a leaking tap", 500, 2500),
            ("Unblock a sink or drain", 1000, 4000),
            ("Install a toilet or basin", 2500, 9000),
            ("Repair a burst pipe", 1500, 8000),
        ],
    ),
    "electrician": (
        "Electrician",
        "Electricians",
        [
            ("Replace sockets and switches", 800, 3500),
            ("Fit a ceiling light or fan", 1000, 4500),
            ("Find a tripping-fuse fault", 1500, 6000),
            ("Rewire a room", 8000, 35000),
        ],
    ),
    "carpenter": (
        "Carpenter",
        "Carpenters",
        [
            ("Fit or repair a door", 2500, 9000),
            ("Build a wardrobe", 15000, 60000),
            ("Repair roof trusses", 10000, 45000),
        ],
    ),
    "tailor": (
        "Tailor",
        "Tailors",
        [
            ("Hem and alter clothes", 300, 1500),
            ("Make a dress or suit", 3000, 15000),
            ("Make curtains, per window", 1500, 6000),
        ],
    ),
    "painter": (
        "Painter",
        "Painters",
        [
            ("Paint a room", 6000, 25000),
            ("Paint one exterior wall", 15000, 70000),
            ("Treat damp and repaint", 8000, 30000),
        ],
    ),
}

MON_SAT = [(d, 8, 17) for d in range(6)]
MON_FRI = [(d, 8, 17) for d in range(5)]
TUE_SAT = [(d, 9, 18) for d in range(1, 6)]
EVERY_DAY = [(d, 8, 18) for d in range(7)]

# name, trade, areas, years, headline, bio,
# badges (phone, id, references, photos), confirmed jobs, mean rating, on-time rate,
# hours to reply, weekly hours, price factor, away this week, extra jobs
# (cancelled by customer, no-show, awaiting a code)
FUNDIS: list[dict[str, Any]] = [
    dict(name="Joseph Mwangi", trade="plumber", areas=["Kilimani", "Kileleshwa"], years=12,
         headline="Leaks, drains and tanks sorted properly",
         bio="Twelve years of plumbing in Kilimani flats and Kileleshwa homes. Gives a written price before he starts.",
         badges=(1, 1, 1, 1), jobs=36, mean=4.8, punctual=0.96, reply=1.5, hours=MON_SAT, factor=1.0,
         away=False, extra=(2, 1, 0)),
    dict(name="Naomi Achieng", trade="plumber", areas=["Kilimani", "Westlands"], years=6,
         headline="Honest plumbing, same-week visits",
         bio="Mostly bathrooms and kitchens. Explains what failed before touching anything.",
         badges=(1, 1, 1, 0), jobs=20, mean=4.6, punctual=0.9, reply=3.0, hours=MON_FRI, factor=1.1,
         away=False, extra=(1, 0, 0)),
    dict(name="Samuel Kiptoo", trade="plumber", areas=["South B", "Embakasi"], years=9,
         headline="Pumps, tanks and burst pipes",
         bio="Works across South B and Embakasi. Slower to reply, careful on site.",
         badges=(1, 1, 0, 0), jobs=14, mean=4.3, punctual=0.8, reply=9.0, hours=MON_SAT, factor=0.9,
         away=True, extra=(1, 1, 0)),
    dict(name="Peter Njoroge", trade="plumber", areas=["Kilimani"], years=3,
         headline="Newer on the platform, one job so far",
         bio="Started taking jobs through Fundi Finder this month. One confirmed job, one review.",
         badges=(1, 0, 0, 0), jobs=1, mean=5.0, punctual=1.0, reply=2.0, hours=MON_FRI, factor=0.85,
         away=False, extra=(0, 0, 0)),
    dict(name="Dorothy Wafula", trade="plumber", areas=["Westlands", "Roysambu"], years=4,
         headline="Taps, basins and toilets",
         bio="Small fixes and installs in Westlands and Roysambu. Three confirmed jobs so far.",
         badges=(1, 1, 0, 0), jobs=3, mean=4.7, punctual=1.0, reply=2.5, hours=MON_FRI, factor=0.95,
         away=False, extra=(0, 0, 0)),
    dict(name="Brian Otieno", trade="electrician", areas=["Westlands", "Kilimani"], years=10,
         headline="Safe wiring, tidy finish, no shortcuts",
         bio="Rewires, fault finding and distribution boards. Switches the power back on only after testing each circuit.",
         badges=(1, 1, 1, 1), jobs=30, mean=4.7, punctual=0.93, reply=2.0, hours=MON_SAT, factor=1.0,
         away=False, extra=(1, 1, 1)),
    dict(name="Esther Wambui", trade="electrician", areas=["Roysambu", "Embakasi"], years=7,
         headline="Faults found and fixed the first visit",
         bio="Fuses that keep tripping, sockets that spark. Works in Roysambu and Embakasi.",
         badges=(1, 1, 0, 1), jobs=12, mean=4.4, punctual=0.85, reply=5.0, hours=MON_SAT, factor=1.05,
         away=False, extra=(1, 0, 0)),
    dict(name="Collins Barasa", trade="electrician", areas=["Kileleshwa", "South B"], years=5,
         headline="Lights, fans and sockets",
         bio="Small electrical jobs. Has run late on some visits; the record below shows how often.",
         badges=(1, 0, 0, 0), jobs=6, mean=3.9, punctual=0.6, reply=14.0, hours=TUE_SAT, factor=0.8,
         away=False, extra=(1, 1, 0)),
    dict(name="Moses Kamau", trade="carpenter", areas=["Westlands", "Kileleshwa"], years=15,
         headline="Doors, wardrobes and roofs, built to measure",
         bio="Fifteen years of joinery. Quotes include the timber, and he says so if a repair is not worth doing.",
         badges=(1, 1, 1, 1), jobs=26, mean=4.8, punctual=0.92, reply=4.0, hours=MON_SAT, factor=1.0,
         away=False, extra=(1, 0, 0)),
    dict(name="Grace Atieno", trade="carpenter", areas=["Embakasi", "South B"], years=8,
         headline="Solid joinery from honest timber",
         bio="Doors, frames and fitted wardrobes in Embakasi and South B.",
         badges=(1, 1, 0, 0), jobs=10, mean=4.5, punctual=0.88, reply=6.0, hours=MON_SAT, factor=0.95,
         away=False, extra=(0, 0, 0)),
    dict(name="Lucy Nduta", trade="tailor", areas=["Kilimani", "Westlands"], years=9,
         headline="Clothes that fit, delivered when promised",
         bio="Alterations, suits and curtains. Measures twice and tells you the pickup day up front.",
         badges=(1, 1, 1, 1), jobs=34, mean=4.9, punctual=0.97, reply=2.0, hours=MON_SAT, factor=1.0,
         away=False, extra=(1, 0, 0)),
    dict(name="Zawadi Chebet", trade="tailor", areas=["Roysambu"], years=4,
         headline="School uniforms and everyday repairs",
         bio="Takes in and lets out, hems, replaces zips. Based in Roysambu.",
         badges=(1, 1, 0, 0), jobs=8, mean=4.5, punctual=0.9, reply=3.5, hours=MON_SAT, factor=0.9,
         away=False, extra=(0, 0, 0)),
    dict(name="Hassan Mohamed", trade="tailor", areas=["South B", "Embakasi"], years=14,
         headline="Suits, kanzus and curtains",
         bio="Fourteen years at the machine. Busy before holidays, so book early.",
         badges=(1, 0, 1, 0), jobs=16, mean=4.2, punctual=0.75, reply=10.0, hours=MON_SAT, factor=1.0,
         away=True, extra=(1, 1, 0)),
    dict(name="Isaac Kibet", trade="painter", areas=["Kileleshwa", "Kilimani"], years=7,
         headline="Clean lines and a finish that lasts",
         bio="Interior rooms and exterior walls. Covers floors and furniture before he opens a tin.",
         badges=(1, 1, 0, 1), jobs=12, mean=4.6, punctual=0.9, reply=5.0, hours=MON_SAT, factor=1.0,
         away=False, extra=(1, 0, 0)),
    dict(name="Janet Njeri", trade="painter", areas=["Westlands"], years=2,
         headline="New to painting jobs, two done here",
         bio="Started painting full time last year. Two confirmed jobs on the platform so far.",
         badges=(1, 0, 0, 0), jobs=2, mean=4.5, punctual=1.0, reply=4.0, hours=EVERY_DAY, factor=0.85,
         away=False, extra=(0, 0, 0)),
]  # fmt: skip

REVIEWERS = [
    "Wanjiru Kimani",
    "Odhiambo Ouma",
    "Mercy Chelangat",
    "Kevin Maina",
    "Faith Akinyi",
    "Daniel Gitau",
    "Rose Naliaka",
    "Brian Koech",
    "Beatrice Mugo",
    "Hassan Ali",
    "Irene Muriuki",
    "Peter Simiyu",
    "Lydia Wafula",
    "George Kariuki",
    "Janet Cheruiyot",
    "Eliud Mbugua",
    "Tabitha Nyambura",
    "Oscar Langat",
    "Violet Jelagat",
    "Nelson Macharia",
    "Zainab Omar",
    "Joseph Karanja",
    "Sharon Adhiambo",
    "Amos Kilonzo",
    "Cecilia Waweru",
    "Martin Odera",
    "Esther Mutheu",
    "Philip Rotich",
    "Naomi Wekesa",
    "Samuel Ndirangu",
]

POSITIVE = [
    "Arrived when he said he would and explained the problem before touching anything.",
    "Neat, quick and honest about the price. The final bill matched the quote.",
    "Fixed it the first time. Cleaned up afterwards.",
    "Very professional from the first call. Would book again.",
    "Careful work and answered every question. Recommended to my neighbours.",
    "Turned up on time, no surprises on the price.",
    "Good finish. Told me what to watch for over the next week.",
    "",
    "",
]
MIXED = [
    "Good work but arrived late and did not call.",
    "Job was fine, a little pricey for what it was.",
    "Took two visits to sort out. Fine in the end.",
]
NEGATIVE = [
    "Left a mess and the problem came back within a week.",
    "Arrived three hours late and asked for more than the quote.",
]
REPLIES = [
    "Thank you. Glad it worked out.",
    "Asante sana. Call me any time.",
    "Thanks for having me. Happy to help again.",
]


# --- clock ---------------------------------------------------------------------------------


def _real_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _Runtime:
    """Module state: the in-memory database and the demo clock."""

    def __init__(self) -> None:
        self.engine: Any = None
        self.maker: sessionmaker[Session] | None = None
        self.offset = timedelta(0)
        self.real_now: Callable[[], datetime] = _real_now

    def now(self) -> datetime:
        return (self.real_now() + self.offset).replace(microsecond=0)

    def session(self) -> Session:
        assert self.maker is not None, "call init() first"
        clock.set_frozen(self.now())
        return self.maker()


RT = _Runtime()


def iso(value: datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat() + "Z" if value else None


# --- seeding -------------------------------------------------------------------------------


def _round50(value: float) -> int:
    return max(50, int(round(value / 50.0)) * 50)


def _pick_rating(rng: random.Random, mean: float) -> int:
    weights = [math.exp(-((r - mean) ** 2) / 0.45) for r in range(1, 6)]
    return rng.choices(range(1, 6), weights=weights)[0]


def _user(
    db: Session,
    name: str,
    email: str,
    phone: str,
    role: str,
    created: datetime,
    *,
    verified: bool = True,
) -> User:
    user = User(
        role=role,
        name=name,
        email=email,
        email_canonical=email,
        phone=phone,
        password_hash="not-a-real-password-hash",
        created_at=created,
        phone_verified_at=created if verified else None,
        is_synthetic=True,
    )
    db.add(user)
    db.flush()
    return user


def seed(db: Session, now: datetime) -> None:
    """Invented people, jobs and reviews. The generator is seeded, so every visitor gets the same data."""
    rng = random.Random(2026)

    def ago(days: float) -> datetime:
        return now - timedelta(days=days)

    trade_rows: dict[str, Trade] = {}
    job_types: dict[str, list[JobType]] = {}
    for slug, (name, plural, types) in TRADES.items():
        trade = Trade(slug=slug, name=name, plural=plural)
        db.add(trade)
        db.flush()
        trade_rows[slug] = trade
        job_types[slug] = []
        for i, (label, _lo, _hi) in enumerate(types):
            jt = JobType(trade_id=trade.id, slug=f"{slug}-{i}", name=label)
            db.add(jt)
            job_types[slug].append(jt)
    area_rows = {a: Area(name=a, slug=a.lower().replace(" ", "-")) for a in AREAS}
    db.add_all(area_rows.values())
    db.flush()

    customers = [
        _user(
            db,
            n,
            f"{n.split()[0].lower()}.{n.split()[-1].lower()}@demo.test",
            f"+2547000{100 + i:05d}",
            Role.CUSTOMER,
            ago(500 + i),
        )
        for i, n in enumerate(REVIEWERS)
    ]
    amina = _user(db, "Amina Wairimu", CUSTOMER_EMAIL, "+254700000001", Role.CUSTOMER, ago(200))
    _user(db, "Demo Admin", ADMIN_EMAIL, "+254700000002", Role.ADMIN, ago(600))

    profiles: dict[str, FundiProfile] = {}
    for idx, spec in enumerate(FUNDIS):
        created = ago(300 if spec["jobs"] > 5 else 20 if spec["jobs"] > 1 else 9)
        slug = spec["trade"]
        user = _user(
            db,
            spec["name"],
            f"{spec['name'].split()[0].lower()}.{spec['name'].split()[-1].lower()}@demo.test",
            f"+2547000{500 + idx:05d}",
            Role.FUNDI,
            created,
        )
        profile = FundiProfile(
            user_id=user.id,
            trade_id=trade_rows[slug].id,
            headline=spec["headline"],
            bio=spec["bio"],
            years_experience=spec["years"],
            created_at=created,
        )
        profile.areas = [area_rows[a] for a in spec["areas"]]
        db.add(profile)
        db.flush()
        profiles[spec["name"]] = profile
        for jt, (_label, lo, hi) in zip(job_types[slug], TRADES[slug][2], strict=True):
            db.add(
                FundiService(
                    fundi_id=profile.id,
                    job_type_id=jt.id,
                    price_min_kes=_round50(lo * spec["factor"]),
                    price_max_kes=_round50(hi * spec["factor"]),
                )
            )
        for weekday, start, end in spec["hours"]:
            db.add(
                AvailabilityRule(
                    fundi_id=profile.id, weekday=weekday, start_hour=start, end_hour=end
                )
            )
        if spec["away"]:
            for d in range(7):
                db.add(
                    AvailabilityException(
                        fundi_id=profile.id,
                        day=(now + timedelta(days=d)).date(),
                        available=False,
                        note="Away this week",
                    )
                )
        phone, id_ok, refs, photos = spec["badges"]
        if not phone:
            user.phone_verified_at = None
        if id_ok:
            db.add(
                IdDocument(
                    fundi_id=profile.id,
                    storage_name="synthetic",
                    status=IdStatus.APPROVED,
                    created_at=created,
                    reviewed_at=created,
                    review_note="Synthetic record",
                )
            )
        if refs:
            for k in range(2):
                db.add(
                    Reference(
                        fundi_id=profile.id,
                        name=f"Reference {k + 1}",
                        phone=f"+2547000{900 + idx * 2 + k:05d}",
                        token=f"demo-token-{idx}-{k}",
                        status=RefStatus.CONFIRMED,
                        created_at=created,
                        confirmed_at=created,
                    )
                )
        if photos:
            for k in range(3):
                db.add(
                    PortfolioPhoto(
                        fundi_id=profile.id,
                        storage_name="synthetic",
                        caption=f"Sample {k + 1}",
                        created_at=created,
                    )
                )
        db.flush()

    def add_job(
        profile: FundiProfile,
        spec: dict[str, Any],
        customer: User,
        *,
        days_ago: float,
        status: str,
        with_review: bool = False,
        jt: JobType | None = None,
    ) -> Job:
        services = sorted(profile.services, key=lambda s: s.id)
        svc = next(
            (s for s in services if jt is not None and s.job_type_id == jt.id), None
        ) or rng.choice(services)
        area = rng.choice(profile.areas)
        price = _round50(rng.uniform(svc.price_min_kes, svc.price_max_kes))
        scheduled = (ago(days_ago)).replace(
            hour=rng.choice([8, 9, 10, 11, 14, 15]), minute=0, second=0, microsecond=0
        )
        reply_h = max(0.3, rng.gauss(spec["reply"], spec["reply"] * 0.3))
        created_req = scheduled - timedelta(days=rng.uniform(1.5, 5))
        first = created_req + timedelta(hours=reply_h)
        req = QuoteRequest(
            customer_id=customer.id,
            fundi_id=profile.id,
            job_type_id=svc.job_type_id,
            area_id=area.id,
            description=f"{svc.job_type.name}. Gate code is with the guard.",
            preferred_time=scheduled,
            status=RequestStatus.ACCEPTED,
            created_at=created_req,
            first_response_at=first,
        )
        db.add(req)
        db.flush()
        quote = Quote(
            request_id=req.id,
            amount_kes=price,
            message="",
            proposed_start=scheduled,
            status=QuoteStatus.ACCEPTED,
            created_at=first,
        )
        db.add(quote)
        db.flush()
        job = Job(
            request_id=req.id,
            quote_id=quote.id,
            customer_id=customer.id,
            fundi_id=profile.id,
            job_type_id=svc.job_type_id,
            area_id=area.id,
            price_kes=price,
            scheduled_for=scheduled,
            status=status,
            created_at=first + timedelta(hours=rng.uniform(1, 8)),
            code_salt=completion.new_salt(),
        )
        on_time = rng.random() < spec["punctual"]
        started = scheduled + (
            timedelta(minutes=rng.randint(-5, 12))
            if on_time
            else timedelta(minutes=rng.randint(25, 90))
        )
        marked = started + timedelta(hours=rng.uniform(1, 4))
        done = marked + timedelta(minutes=rng.randint(5, 90))
        if status in (JobStatus.IN_PROGRESS, JobStatus.AWAITING_CONFIRMATION, JobStatus.COMPLETED):
            job.started_at = started
        if status in (JobStatus.AWAITING_CONFIRMATION, JobStatus.COMPLETED):
            job.marked_complete_at = marked
        if status == JobStatus.COMPLETED:
            job.completed_at = done
            job.code_used_at = done
            job.code_confirmed_by = "customer" if rng.random() < 0.8 else "fundi"
        db.add(job)
        db.flush()
        if with_review and status == JobStatus.COMPLETED:
            rating = _pick_rating(rng, spec["mean"])
            pool = POSITIVE if rating >= 4 else MIXED if rating == 3 else NEGATIVE
            when = min(done + timedelta(hours=rng.uniform(1, 40)), now - timedelta(minutes=5))
            review = Review(
                job_id=job.id,
                customer_id=customer.id,
                fundi_id=profile.id,
                rating=rating,
                body=rng.choice(pool),
                created_at=when,
            )
            if rating >= 4 and rng.random() < 0.3:
                review.reply_body = rng.choice(REPLIES)
                review.reply_at = when + timedelta(hours=rng.uniform(1, 20))
            db.add(review)
        return job

    # Past, confirmed jobs with reviews, plus jobs that did not complete.
    for spec in FUNDIS:
        profile = profiles[spec["name"]]
        n = spec["jobs"]
        horizon = 330 if n > 5 else 25 if n > 1 else 6
        for _ in range(n):
            customer = rng.choice(customers)
            add_job(
                profile,
                spec,
                customer,
                days_ago=rng.uniform(4 if n > 5 else 3, horizon),
                status=JobStatus.COMPLETED,
                with_review=rng.random() < 0.88,
            )
        cancelled, no_show, awaiting = spec["extra"]
        for _ in range(cancelled):
            job = add_job(
                profile,
                spec,
                rng.choice(customers),
                days_ago=rng.uniform(10, 120),
                status=JobStatus.CANCELLED,
            )
            job.cancelled_by = "customer"
            job.cancel_reason = "Cancelled by customer"
        for _ in range(no_show):
            add_job(
                profile,
                spec,
                rng.choice(customers),
                days_ago=rng.uniform(20, 150),
                status=JobStatus.NO_SHOW,
            )
        for _ in range(awaiting):
            # Work marked done a few days ago; the customer has not entered the code, so no review is possible.
            add_job(
                profile,
                spec,
                rng.choice(customers),
                days_ago=rng.uniform(2, 6),
                status=JobStatus.AWAITING_CONFIRMATION,
            )
    db.flush()

    # Amina's bookings, one in each state worth trying.
    def by(name: str) -> tuple[FundiProfile, dict[str, Any]]:
        return profiles[name], next(s for s in FUNDIS if s["name"] == name)

    # 1. Request waiting for a quote.
    naomi, _ = by("Naomi Achieng")
    tap = next(s for s in naomi.services if s.job_type.name == "Fix a leaking tap")
    db.add(
        QuoteRequest(
            customer_id=amina.id,
            fundi_id=naomi.id,
            job_type_id=tap.job_type_id,
            area_id=naomi.areas[0].id,
            description="Basin tap in the bathroom drips all night.",
            preferred_time=now + timedelta(days=2, hours=1),
            created_at=now - timedelta(hours=3),
        )
    )

    # 2. Booked, not started.
    brian, brian_spec = by("Brian Otieno")
    sockets = next(
        jt for jt in job_types["electrician"] if jt.name == "Replace sockets and switches"
    )
    j = add_job(brian, brian_spec, amina, days_ago=-1.2, status=JobStatus.BOOKED, jt=sockets)
    j.request.description = "Two sockets in the lounge are loose and one is warm."
    j.request.created_at = now - timedelta(days=2)
    j.request.first_response_at = now - timedelta(days=2) + timedelta(hours=1)
    quote = db.get(Quote, j.quote_id)
    assert quote is not None
    quote.created_at = j.request.first_response_at
    j.created_at = now - timedelta(days=1, hours=20)

    # 3. Work marked done, waiting for the customer's code. The main thing to try.
    joseph, joseph_spec = by("Joseph Mwangi")
    sink = next(jt for jt in job_types["plumber"] if jt.name == "Unblock a sink or drain")
    j = add_job(
        joseph, joseph_spec, amina, days_ago=0.2, status=JobStatus.AWAITING_CONFIRMATION, jt=sink
    )
    j.request.description = "Kitchen sink drains very slowly."
    j.request.created_at = now - timedelta(days=3)
    j.request.first_response_at = now - timedelta(days=3) + timedelta(hours=1)
    quote = db.get(Quote, j.quote_id)
    assert quote is not None
    quote.created_at = j.request.first_response_at
    j.created_at = now - timedelta(days=2, hours=20)
    j.scheduled_for = now - timedelta(hours=5)
    quote.proposed_start = j.scheduled_for
    j.request.preferred_time = j.scheduled_for
    j.started_at = now - timedelta(hours=4, minutes=50)
    j.marked_complete_at = now - timedelta(minutes=40)

    # 4. Confirmed with a code, not yet reviewed.
    lucy, lucy_spec = by("Lucy Nduta")
    curtains = next(jt for jt in job_types["tailor"] if jt.name == "Make curtains, per window")
    j = add_job(lucy, lucy_spec, amina, days_ago=3, status=JobStatus.COMPLETED, jt=curtains)
    j.request.description = "Two bedroom windows, lined, to hang by Friday."
    db.flush()
    db.commit()


# --- queries and serialisers ---------------------------------------------------------------


def _customer(db: Session) -> User:
    return db.scalars(select(User).where(User.email == CUSTOMER_EMAIL)).one()


def _admin(db: Session) -> User:
    return db.scalars(select(User).where(User.email == ADMIN_EMAIL)).one()


def _card(c: queries.FundiCard, areas: bool = True) -> dict[str, Any]:
    p = c.profile
    out: dict[str, Any] = {
        "id": p.id,
        "name": p.user.name,
        "first_name": p.user.first_name,
        "trade": p.trade.name,
        "trade_id": p.trade_id,
        "headline": p.headline,
        "years": p.years_experience,
        "rating": round(c.raw_average, 1) if c.raw_average is not None else None,
        "review_count": c.review_count,
        "completed_jobs": c.completed_jobs,
        "badges": [
            {"key": b.key.value, "label": b.label, "why": b.earned_because} for b in c.badges
        ],
        "price_min": c.price_min,
        "price_max": c.price_max,
        "available_this_week": c.available_this_week,
        "is_new": c.is_new,
        "boosted": c.boosted,
        "phone_verified": c.phone_verified,
        "score": round(c.score, 1),
        "median_reply_hours": round(c.median_response_hours, 1)
        if c.median_response_hours is not None
        else None,
    }
    if areas:
        out["areas"] = [a.name for a in p.areas]
    return out


def _filters(args: dict[str, Any]) -> queries.SearchFilters:
    def num(key: str) -> int | None:
        value = args.get(key)
        return int(value) if value not in (None, "", 0, "0") else None

    return queries.SearchFilters(
        trade_id=num("trade_id"),
        area_id=num("area_id"),
        verified_only=bool(args.get("verified_only")),
        available_week=bool(args.get("available_week")),
        min_price=None,
        max_price=num("max_price"),
    )


def catalog(db: Session, _args: dict[str, Any]) -> dict[str, Any]:
    trades = db.scalars(select(Trade).order_by(Trade.name)).all()
    areas = db.scalars(select(Area).order_by(Area.name)).all()
    return {
        "trades": [{"id": t.id, "name": t.name, "plural": t.plural} for t in trades],
        "areas": [{"id": a.id, "name": a.name} for a in areas],
        "customer": {"name": _customer(db).name},
        "now": iso(RT.now()),
        "platform": {
            "fundis": db.scalar(select(func.count(FundiProfile.id))),
            "confirmed_jobs": db.scalar(
                select(func.count(Job.id)).where(Job.status == JobStatus.COMPLETED)
            ),
            "reviews": db.scalar(select(func.count(Review.id))),
        },
        "limits": {
            "wrong_before_lock": completion.MAX_FAILURES_PER_WINDOW,
            "lock_minutes": int(completion.LOCKOUT.total_seconds() // 60),
            "wrong_before_disabled": completion.MAX_TOTAL_FAILURES,
            "review_window_days": reviews.REVIEW_WINDOW.days,
            "edit_window_days": reviews.EDIT_WINDOW.days,
            "new_fundi_jobs": ranking.NEW_FUNDI_JOB_THRESHOLD,
        },
    }


def search(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    cards = queries.search_fundis(db, _filters(args), RT.now())
    return {"results": [_card(c) for c in cards]}


def _record(db: Session, profile: FundiProfile) -> dict[str, Any]:
    def count(*where: Any) -> int:
        return int(
            db.scalar(select(func.count(Job.id)).where(Job.fundi_id == profile.id, *where)) or 0
        )

    confirmed = count(Job.status == JobStatus.COMPLETED, Job.code_used_at.is_not(None))
    in_flight = count(Job.status.in_([JobStatus.BOOKED, JobStatus.IN_PROGRESS]))
    awaiting = count(Job.status == JobStatus.AWAITING_CONFIRMATION)
    cancelled = count(Job.status == JobStatus.CANCELLED)
    no_show = count(Job.status == JobStatus.NO_SHOW)
    published = int(
        db.scalar(
            select(func.count(Review.id)).where(
                Review.fundi_id == profile.id, Review.status == ReviewStatus.PUBLISHED
            )
        )
        or 0
    )
    # Any review attached to a job that is not confirmed with a used code. The gate makes this impossible.
    stray = int(
        db.scalar(
            select(func.count(Review.id))
            .join(Job, Job.id == Review.job_id)
            .where(
                Review.fundi_id == profile.id,
                (Job.status != JobStatus.COMPLETED) | Job.code_used_at.is_(None),
            )
        )
        or 0
    )
    return {
        "bookings": count(),
        "confirmed": confirmed,
        "in_flight": in_flight,
        "awaiting_code": awaiting,
        "cancelled": cancelled,
        "no_show": no_show,
        "reviews": published,
        "reviews_without_confirmed_job": stray,
        "confirmed_without_review": confirmed - published,
    }


def profile(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    p = db.get(FundiProfile, int(args["id"]))
    if p is None:
        raise RuleViolation("There is no fundi with that number.")
    card = queries.load_cards(db, [p], RT.now())[0]
    bench = queries.benchmarks_by_job_type(db)
    rows = db.scalars(
        select(Review)
        .where(Review.fundi_id == p.id, Review.status == ReviewStatus.PUBLISHED)
        .options(selectinload(Review.customer), selectinload(Review.job).selectinload(Job.job_type))
        .order_by(Review.created_at.desc())
    ).all()
    rules = [
        WeeklyRule(r.weekday, r.start_hour, r.end_hour)
        for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.fundi_id == p.id))
    ]
    stats = card.stats
    out = _card(card)
    out.update(
        bio=p.bio,
        hours=summarise(rules),
        phone=display_phone(p.user.phone),
        histogram=[
            {"stars": s, "count": n} for s, n in sorted(card.histogram.items(), reverse=True)
        ],
        components=[{"label": k, "points": v} for k, v in card.breakdown.components.items()],
        record={
            **_record(db, p),
            "completed_90d": queries.recent_completed_jobs(db, p.id, RT.now()),
            "on_time": [stats.punctual_jobs, stats.punctual_eligible],
            "replied_in_time": [stats.requests_answered_in_time, stats.requests_answerable],
        },
        services=[
            {
                "job_type_id": s.job_type_id,
                "name": s.job_type.name,
                "min": s.price_min_kes,
                "max": s.price_max_kes,
                "bench": (
                    {
                        "median": bench[s.job_type_id].median_kes,
                        "low": bench[s.job_type_id].low_kes,
                        "high": bench[s.job_type_id].high_kes,
                        "n": bench[s.job_type_id].sample_size,
                    }
                    if s.job_type_id in bench
                    else None
                ),
            }
            for s in p.services
        ],
        area_options=[{"id": a.id, "name": a.name} for a in p.areas],
        reviews=[
            {
                "id": r.id,
                "who": _reviewer_label(r.customer.name),
                "rating": r.rating,
                "body": r.body,
                "date": iso(r.created_at),
                "job": r.job.job_type.name,
                "confirmed_at": iso(r.job.completed_at),
                "reply": r.reply_body,
            }
            for r in rows[:12]
        ],
        platform_mean=round(ranking.DEFAULT_PLATFORM_MEAN, 1),
        pillars=[
            {"label": "Phone verified", "have": card.phone_verified},
            {"label": "ID checked", "have": any(b.key.value == "id" for b in card.badges)},
            {"label": "References", "have": any(b.key.value == "references" for b in card.badges)},
            {"label": "Trade skills", "have": any(b.key.value == "skills" for b in card.badges)},
        ],
    )
    return out


def _reviewer_label(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


def _job_view(db: Session, job: Job, customer: User) -> dict[str, Any]:
    status = JobStatus(job.status)
    show_code = status in (JobStatus.BOOKED, JobStatus.IN_PROGRESS, JobStatus.AWAITING_CONFIRMATION)
    now = RT.now()
    locked = job.code_locked_until is not None and now < job.code_locked_until
    review = job.review
    block = reviews.review_block_reason(db, customer, job)
    left = max(0, completion.MAX_FAILURES_PER_WINDOW - job.code_failures)
    return {
        "id": job.id,
        "status": status.value,
        "price": job.price_kes,
        "scheduled_for": iso(job.scheduled_for),
        "started_at": iso(job.started_at),
        "marked_complete_at": iso(job.marked_complete_at),
        "completed_at": iso(job.completed_at),
        "confirmed_by": job.code_confirmed_by,
        "code": completion.derive_code(DEMO_SECRET, job.code_salt)
        if show_code and not job.code_invalidated
        else None,
        "code_used": job.code_used_at is not None,
        "code_locked_until": iso(job.code_locked_until) if locked else None,
        "code_invalidated": bool(job.code_invalidated),
        "attempts_left": left,
        "total_failures": job.code_total_failures,
        "customer_events": [e.value for e in allowed_events(status, Actor.CUSTOMER)],
        "fundi_events": [e.value for e in allowed_events(status, Actor.FUNDI)],
        "terminal": status in TERMINAL,
        "can_review": block is None,
        "review_block": block,
        "review": (
            {"rating": review.rating, "body": review.body, "date": iso(review.created_at)}
            if review is not None
            else None
        ),
    }


def bookings(db: Session, _args: dict[str, Any]) -> dict[str, Any]:
    customer = _customer(db)
    reqs = db.scalars(
        select(QuoteRequest)
        .where(QuoteRequest.customer_id == customer.id)
        .options(
            selectinload(QuoteRequest.fundi).selectinload(FundiProfile.user),
            selectinload(QuoteRequest.quotes),
            selectinload(QuoteRequest.job_type),
            selectinload(QuoteRequest.area),
        )
        .order_by(QuoteRequest.created_at.desc())
    ).all()
    items = []
    for req in reqs:
        job = db.scalar(select(Job).where(Job.request_id == req.id))
        quote = next(
            (
                q
                for q in reversed(req.quotes)
                if q.status in (QuoteStatus.SENT, QuoteStatus.ACCEPTED)
            ),
            None,
        )
        items.append(
            {
                "request_id": req.id,
                "request_status": req.status,
                "fundi": {
                    "id": req.fundi_id,
                    "name": req.fundi.user.name,
                    "first_name": req.fundi.user.first_name,
                },
                "job_type": req.job_type.name,
                "area": req.area.name,
                "description": req.description,
                "preferred_time": iso(req.preferred_time),
                "created_at": iso(req.created_at),
                "quote": (
                    {
                        "id": quote.id,
                        "amount": quote.amount_kes,
                        "message": quote.message,
                        "start": iso(quote.proposed_start),
                        "status": quote.status,
                    }
                    if quote
                    else None
                ),
                "job": _job_view(db, job, customer) if job else None,
            }
        )

    # Active work first, then finished; newest first within each.
    def finished(item: dict[str, Any]) -> bool:
        job = item["job"]
        return bool(job and job["terminal"]) or item["request_status"] in ("cancelled", "declined")

    items.sort(key=lambda i: (finished(i), -_ts(i["created_at"])))
    return {"items": items, "now": iso(RT.now())}


def _ts(value: str | None) -> float:
    return (
        datetime.fromisoformat((value or "1970-01-01T00:00:00Z").rstrip("Z"))
        .replace(tzinfo=UTC)
        .timestamp()
    )


# --- actions -------------------------------------------------------------------------------


def _parse_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuleViolation("Choose a date and time.") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def request_quote(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    customer = _customer(db)
    profile_ = db.get(FundiProfile, int(args["fundi_id"]))
    if profile_ is None:
        raise RuleViolation("There is no fundi with that number.")
    description = str(args.get("description", "")).strip()
    if len(description) < 15:
        raise RuleViolation("Description needs at least 15 characters.")
    req = marketplace.create_request(
        db,
        customer,
        profile_,
        int(args["job_type_id"]),
        int(args["area_id"]),
        description[:1500],
        _parse_time(str(args["preferred_time"])),
    )
    db.commit()
    return {"request_id": req.id, "fundi_first_name": profile_.user.first_name}


def send_quote(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    req = db.get(QuoteRequest, int(args["request_id"]))
    if req is None:
        raise RuleViolation("There is no such request.")
    try:
        amount = int(str(args["amount"]).replace(",", "").strip())
    except ValueError as exc:
        raise RuleViolation("Enter the price as a whole number of shillings.") from exc
    quote = marketplace.send_quote(
        db,
        req.fundi.user,
        req,
        amount,
        str(args.get("message", ""))[:500],
        _parse_time(str(args["start"])),
    )
    db.commit()
    return {"quote_id": quote.id}


def decline_request(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    req = db.get(QuoteRequest, int(args["request_id"]))
    if req is None:
        raise RuleViolation("There is no such request.")
    marketplace.decline_request(db, req.fundi.user, req)
    db.commit()
    return {}


def cancel_request(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    req = db.get(QuoteRequest, int(args["request_id"]))
    if req is None:
        raise RuleViolation("There is no such request.")
    marketplace.cancel_request(db, _customer(db), req)
    db.commit()
    return {}


def accept_quote(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    quote = db.get(Quote, int(args["quote_id"]))
    if quote is None:
        raise RuleViolation("There is no such quote.")
    job = marketplace.accept_quote(db, _customer(db), quote)
    db.commit()
    return {"job_id": job.id}


def _actor_user(db: Session, job: Job, actor: str) -> User:
    if actor == "customer":
        return _customer(db)
    if actor == "fundi":
        return job.fundi.user
    raise RuleViolation("Unknown actor.")


def job_event(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    job = db.get(Job, int(args["job_id"]))
    if job is None:
        raise RuleViolation("There is no such job.")
    user = _actor_user(db, job, str(args["actor"]))
    marketplace.apply_event(db, user, job, Event(str(args["event"])), str(args.get("reason", "")))
    db.commit()
    return {"status": job.status}


def confirm(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    """Enter the completion code as the customer, or as the fundi typing what the customer read out."""
    job = db.get(Job, int(args["job_id"]))
    if job is None:
        raise RuleViolation("There is no such job.")
    user = _actor_user(db, job, str(args["actor"]))
    result = marketplace.confirm_completion(db, user, job, str(args.get("code", "")), DEMO_SECRET)
    text = CODE_MESSAGES[result.result].format(left=result.attempts_left)
    return {
        "result": result.result.value,
        "message": text,
        "attempts_left": result.attempts_left,
        "locked_until": iso(result.locked_until),
    }


def probe_reuse(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    """Call the real single-use guard on a job that is already confirmed.

    In the app a completed job never reaches the code check again (the rule above it refuses
    first), so this calls ``completion.check_code`` directly to show what it would answer.
    """
    job = db.get(Job, int(args["job_id"]))
    if job is None:
        raise RuleViolation("There is no such job.")
    result = completion.check_code(job, str(args.get("code", "")), DEMO_SECRET, RT.now())
    db.rollback()
    return {
        "result": result.result.value,
        "message": CODE_MESSAGES[result.result].format(left=result.attempts_left),
    }


def reissue(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    job = db.get(Job, int(args["job_id"]))
    if job is None:
        raise RuleViolation("There is no such job.")
    completion.reissue(job)
    audit.log(db, _admin(db), "job.reissue_code", "job", job.id, "Demo admin issued a new code")
    db.commit()
    return {}


def _fundi_numbers(db: Session, fundi_id: int) -> dict[str, Any]:
    fundi = db.get(FundiProfile, fundi_id)
    assert fundi is not None
    card = queries.load_cards(db, [fundi], RT.now())[0]
    return {
        "review_count": card.review_count,
        "rating": round(card.raw_average, 2) if card.raw_average else None,
        "score": round(card.score, 1),
    }


def review(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    job = db.get(Job, int(args["job_id"]))
    if job is None:
        raise RuleViolation("There is no such job.")
    before = _fundi_numbers(db, job.fundi_id)
    try:
        rating = int(args.get("rating") or 0)
    except ValueError:
        rating = 0
    # A missing rating is passed on as 0 so the gate is checked first, as in the real app.
    reviews.create_review(db, _customer(db), job, rating, str(args.get("body", "")).strip())
    db.commit()
    return {
        "before": before,
        "after": _fundi_numbers(db, job.fundi_id),
        "fundi": job.fundi.user.name,
    }


def advance_clock(_db: Session, args: dict[str, Any]) -> dict[str, Any]:
    hours = float(args["hours"])
    RT.offset += timedelta(hours=hours)
    return {"now": iso(RT.now())}


# --- lifecycle of the in-memory database ---------------------------------------------------


def _build(now: datetime | None = None) -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    RT.engine = engine
    RT.maker = sessionmaker(engine, expire_on_commit=False)
    RT.offset = timedelta(0)
    if now is not None:
        RT.real_now = lambda: now
    clock.set_frozen(RT.now())
    with RT.maker() as db:
        seed(db, RT.now())


def init(saved: str | None = None, now_iso: str | None = None) -> str:
    """Start the engine: restore saved state if it is valid, otherwise seed fresh data."""
    if now_iso:
        fixed = datetime.fromisoformat(now_iso)
        RT.real_now = lambda: fixed
    restored = False
    if saved:
        try:
            restored = _restore(saved)
        except Exception:  # a bad or old save is not an error: start again
            restored = False
    if not restored:
        _build()
    return json.dumps({"restored": restored, "version": STATE_VERSION})


def reset() -> str:
    _build()
    return json.dumps({"ok": True})


def export_state() -> str:
    raw = RT.engine.raw_connection()
    try:
        sql = "\n".join(raw.driver_connection.iterdump())
    finally:
        raw.close()
    return json.dumps({"v": STATE_VERSION, "demo_now": iso(RT.now()), "sql": sql})


def _restore(saved: str) -> bool:
    data = json.loads(saved)
    if data.get("v") != STATE_VERSION or not data.get("sql"):
        return False
    engine = make_engine("sqlite://")
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(data["sql"])
        conn.execute("PRAGMA foreign_keys=ON")
        raw.commit()
    finally:
        raw.close()
    RT.engine = engine
    RT.maker = sessionmaker(engine, expire_on_commit=False)
    saved_now = datetime.fromisoformat(data["demo_now"].rstrip("Z"))
    RT.offset = saved_now - RT.real_now()
    return True


ROUTES: dict[str, Callable[[Session, dict[str, Any]], dict[str, Any]]] = {
    "catalog": catalog,
    "search": search,
    "profile": profile,
    "bookings": bookings,
    "request_quote": request_quote,
    "send_quote": send_quote,
    "decline_request": decline_request,
    "cancel_request": cancel_request,
    "accept_quote": accept_quote,
    "job_event": job_event,
    "confirm": confirm,
    "probe_reuse": probe_reuse,
    "reissue": reissue,
    "review": review,
    "advance_clock": advance_clock,
}


def call(name: str, payload: str = "{}") -> str:
    """Single entry point for the page: JSON in, JSON out. Rule refusals come back as ok=false."""
    handler = ROUTES[name]
    args = json.loads(payload or "{}")
    db = RT.session()
    try:
        data = handler(db, args)
        return json.dumps({"ok": True, "data": data})
    except RuleViolation as exc:
        db.rollback()
        return json.dumps({"ok": False, "error": str(exc), "kind": "rule"})
    except Exception as exc:  # shown to the visitor instead of failing silently
        db.rollback()
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "bug"})
    finally:
        db.close()
