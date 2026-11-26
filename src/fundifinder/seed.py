"""Synthetic demo data: ~60 fundis, ~400 jobs, ~300 reviews, three planted review rings.

Everything here is invented. Names are random combinations, phone numbers sit in the
0700 000 xxx placeholder block, emails use the reserved .test domain, and every image is a
generated placeholder. The random generator is seeded, so the same data comes out each time.
"""

from __future__ import annotations

import io
import random
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from flask import Flask
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import abuse_scan, clock
from .completion import derive_code
from .db import get_db
from .models import (
    AbuseFlag,
    AppealStatus,
    Area,
    AuditLog,
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
    Report,
    ReportStatus,
    RequestStatus,
    Review,
    Role,
    Trade,
    User,
)
from .security import hash_password
from .uploads import Storage

DEMO_PASSWORD = "demo-password-123"

AREAS = [
    "Kilimani", "Westlands", "Kileleshwa", "Lavington", "South B", "South C", "Langata",
    "Karen", "Embakasi", "Kasarani", "Roysambu", "Ruaka", "Donholm", "Buruburu",
    "Eastleigh", "Ngong Road",
]  # fmt: skip

# slug -> (name, plural, [(job type, min KES, max KES)])
TRADES: dict[str, tuple[str, str, list[tuple[str, int, int]]]] = {
    "plumber": ("Plumber", "Plumbers", [
        ("Fix a leaking tap", 500, 2500), ("Unblock a sink or drain", 1000, 4000),
        ("Install a toilet or basin", 2500, 9000), ("Connect a water tank and pump", 4000, 18000),
        ("Repair a burst pipe", 1500, 8000)]),
    "electrician": ("Electrician", "Electricians", [
        ("Replace sockets and switches", 800, 3500), ("Rewire a room", 8000, 35000),
        ("Fit a ceiling light or fan", 1000, 4500), ("Find a tripping-fuse fault", 1500, 6000),
        ("Install a distribution board", 6000, 25000)]),
    "mason": ("Mason", "Masons", [
        ("Build a boundary wall section", 25000, 120000), ("Plaster a room", 6000, 25000),
        ("Lay floor tiles in a room", 8000, 40000), ("Repair a cracked wall", 3000, 15000)]),
    "carpenter": ("Carpenter", "Carpenters", [
        ("Fit or repair a door", 2500, 9000), ("Build a wardrobe", 15000, 60000),
        ("Repair roof trusses", 10000, 45000), ("Make kitchen cabinets", 30000, 120000)]),
    "tailor": ("Tailor", "Tailors", [
        ("Hem and alter clothes", 300, 1500), ("Make a dress or suit", 3000, 15000),
        ("Make curtains, per window", 1500, 6000), ("School uniform set", 2500, 6500)]),
    "painter": ("Painter", "Painters", [
        ("Paint a room", 6000, 25000), ("Paint one exterior wall", 15000, 70000),
        ("Treat damp and repaint", 8000, 30000)]),
    "welder": ("Welder", "Welders", [
        ("Fit or repair a gate", 4000, 25000), ("Make a window grille", 5000, 20000),
        ("Fabricate a security door", 15000, 55000)]),
    "appliance-repair": ("Appliance repairer", "Appliance repairers", [
        ("Fix a washing machine", 1500, 6000), ("Repair a fridge", 2000, 8000),
        ("Fix a microwave or cooker", 1000, 4500), ("Repair a television", 1500, 7000)]),
}  # fmt: skip

TRADE_WEIGHTS = {
    "plumber": 12, "electrician": 11, "mason": 7, "carpenter": 7,
    "tailor": 8, "painter": 6, "welder": 5, "appliance-repair": 4,
}  # fmt: skip

FIRST = [
    "Amos", "Brian", "Cecilia", "Daniel", "Esther", "Felix", "Grace", "Hassan", "Irene",
    "Joseph", "Kevin", "Lucy", "Moses", "Naomi", "Oscar", "Peter", "Quincy", "Rose",
    "Samuel", "Tabitha", "Urbanus", "Violet", "Wycliffe", "Yusuf", "Zawadi", "Abdi",
    "Beatrice", "Collins", "Dorothy", "Eliud", "Faith", "George", "Halima", "Isaac",
    "Janet", "Kamau", "Lydia", "Mercy", "Nelson", "Otieno",
]  # fmt: skip
LAST = [
    "Kamau", "Otieno", "Wanjiku", "Mwangi", "Njoroge", "Achieng", "Kiptoo", "Mutua",
    "Wafula", "Ndungu", "Chebet", "Omondi", "Karanja", "Akinyi", "Kilonzo", "Barasa",
    "Mohamed", "Waweru", "Nyambura", "Kariuki", "Odhiambo", "Muriuki", "Wambui",
    "Maina", "Cheruiyot", "Atieno", "Gitau", "Simiyu", "Jelagat", "Mugo", "Ouma",
    "Nduta", "Kimani", "Langat", "Naliaka", "Kibet", "Adhiambo", "Macharia", "Koech", "Mbugua",
]  # fmt: skip

HEADLINES = {
    "plumber": ["Leaks, drains and tanks sorted properly", "Honest plumbing, same-week visits"],
    "electrician": ["Safe wiring, tidy finish, no shortcuts", "Faults found and fixed the first visit"],
    "mason": ["Walls, plaster and tiles built to last", "Neat masonry, fair quotes up front"],
    "carpenter": ["Doors, wardrobes and roofs, built to measure", "Solid joinery from honest timber"],
    "tailor": ["Clothes that fit, delivered when promised", "Alterations, suits and curtains"],
    "painter": ["Clean lines and a finish that lasts", "Interior and exterior painting"],
    "welder": ["Gates, grilles and doors made strong", "Fabrication to your drawing or mine"],
    "appliance-repair": ["Washers, fridges and cookers back to work", "Repairs that explain what failed"],
}  # fmt: skip

POS_OPEN = [
    "Arrived exactly when he said he would.", "Very professional from the first call.",
    "Neat, quick and honest about the price.", "Explained the problem before touching anything.",
    "Fixed it the first time.", "Turned up on time and cleaned up afterwards.",
    "Fair price and good work.", "Careful and tidy, and answered every question.",
]  # fmt: skip
POS_CLOSE = [
    "Will call again.", "Recommended to my neighbours.", "Price matched the quote.",
    "No surprises.", "Very happy.", "", "", "Booked him for the next job already.",
]  # fmt: skip
MID = [
    "Good work but he arrived late.", "Job was fine, a little pricey for what it was.",
    "Took two visits to sort out. Okay in the end.", "Fine overall. Communication could be better.",
]  # fmt: skip
NEG = [
    "Left a mess and the problem came back within a week.",
    "Arrived three hours late and wanted more than the quote.",
    "Poor finish. I had to pay someone else to redo it.",
]  # fmt: skip
REPLIES_POS = [
    "Thank you, glad it worked out.", "Asante sana. Call me any time.",
    "Thanks for having me. Happy to help again.",
]  # fmt: skip
REPLIES_NEG = [
    "I am sorry about this. I have messaged you to put it right.",
    "Apologies for the delay that day. Traffic on Ngong Road was bad and I should have called.",
]  # fmt: skip
RING_TEXT = [
    "Great work! Highly recommended!!", "Best fundi in Nairobi, 5 stars.",
    "Excellent service, very good, recommend.", "Super professional. Five stars!",
]  # fmt: skip

TRADE_COLOURS = {
    "plumber": (60, 130, 190), "electrician": (230, 170, 40), "mason": (160, 110, 80),
    "carpenter": (150, 100, 55), "tailor": (170, 80, 120), "painter": (70, 150, 120),
    "welder": (200, 90, 50), "appliance-repair": (90, 100, 160),
}  # fmt: skip


@dataclass
class SeedSummary:
    fundis: int = 0
    customers: int = 0
    jobs: int = 0
    completed_jobs: int = 0
    reviews: int = 0
    reports: int = 0
    id_documents: int = 0
    abuse_flags: int = 0
    rings: dict[str, int] = field(default_factory=dict)


class SeedError(RuntimeError):
    pass


@dataclass
class Sim:
    profile: FundiProfile
    user: User
    quality: float
    p_on_time: float
    p_fast: float
    delay_hours: float
    n_jobs: int
    area_ids: list[int]
    services: list[FundiService]
    created: datetime


# ---- placeholder images (Pillow only, no external assets) ------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def placeholder_work_image(trade: str, variant: int) -> bytes:
    r, g, b = TRADE_COLOURS[trade]
    img = Image.new("RGB", (640, 480), (r, g, b))
    d = ImageDraw.Draw(img)
    shade = (max(r - 40, 0), max(g - 40, 0), max(b - 40, 0))
    d.rectangle((0, 300 + variant * 12, 640, 480), fill=shade)
    for i in range(6):
        x = 40 + ((i * 97 + variant * 53) % 520)
        d.ellipse((x, 70 + (i * 41) % 160, x + 60, 130 + (i * 41) % 160), fill=(255, 255, 255, 40))
    d.rounded_rectangle((170, 170, 470, 310), radius=18, fill=(255, 255, 255))
    d.text((320, 215), "SAMPLE IMAGE", fill=(20, 33, 61), font=_font(30), anchor="mm")
    d.text(
        (320, 262),
        f"{TRADES[trade][0]} portfolio #{variant + 1}",
        fill=(90, 100, 120),
        font=_font(20),
        anchor="mm",
    )
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def placeholder_id_image(name: str) -> bytes:
    img = Image.new("RGB", (640, 400), (236, 232, 220))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 640, 56), fill=(20, 33, 61))
    d.text(
        (24, 28),
        "SYNTHETIC PLACEHOLDER: NOT A REAL ID",
        fill=(252, 163, 17),
        font=_font(22),
        anchor="lm",
    )
    d.rectangle((28, 90, 200, 290), outline=(120, 120, 120), width=3)
    d.text((114, 190), "PHOTO", fill=(150, 150, 150), font=_font(24), anchor="mm")
    d.text((230, 120), f"Name: {name}", fill=(40, 40, 40), font=_font(24))
    d.text((230, 170), "ID no: 00000000", fill=(40, 40, 40), font=_font(24))
    d.text((230, 220), "Issued: demo", fill=(40, 40, 40), font=_font(24))
    d.text(
        (320, 350),
        "Generated for the Fundi Finder demo",
        fill=(120, 120, 120),
        font=_font(18),
        anchor="mm",
    )
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---- the generator ---------------------------------------------------------------------


class Seeder:
    def __init__(self, app: Flask, rng_seed: int) -> None:
        self.app = app
        self.db: Session = get_db()
        self.rng = random.Random(rng_seed)
        self.now = clock.now()
        self.storage: Storage = app.extensions["storage"]
        self.secret = str(app.config["SECRET_KEY"])
        self.password_hash = hash_password(DEMO_PASSWORD, app.config["PASSWORD_HASH_METHOD"])
        self.summary = SeedSummary()
        self.areas: list[Area] = []
        self.trades: dict[str, Trade] = {}
        self.job_types: dict[str, list[JobType]] = {}
        self.sims: list[Sim] = []
        self.customers: list[User] = []
        self.admins: list[User] = []
        self._phone_pool = list(range(1, 800))
        self.rng.shuffle(self._phone_pool)
        self._ip_pool = [f"{self.rng.getrandbits(64):016x}" for _ in range(1500)]
        self._used_names: set[str] = set()
        self._photo_cache: dict[tuple[str, int], bytes] = {}
        self.honest_reviews: list[Review] = []

    # -- primitives --

    def ago(self, days: float) -> datetime:
        return self.now - timedelta(days=days)

    def phone(self, n: int | None = None) -> str:
        n = self._phone_pool.pop() if n is None else n
        return f"+254700000{n:03d}"

    def name(self) -> str:
        while True:
            full = f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)}"
            if full not in self._used_names:
                self._used_names.add(full)
                return full

    def user(
        self,
        name: str,
        role: str,
        created: datetime,
        *,
        email: str | None = None,
        phone: str | None = None,
        ip: str | None = None,
        verified: bool = True,
    ) -> User:
        slug = name.lower().replace(" ", ".")
        u = User(
            role=role,
            name=name,
            email=email or f"{slug}.{self.rng.randrange(100, 999)}@example.test",
            phone=phone or self.phone(),
            password_hash=self.password_hash,
            created_at=created,
            signup_ip_hash=ip or self.rng.choice(self._ip_pool),
            phone_verified_at=created + timedelta(minutes=5) if verified else None,
            is_synthetic=True,
        )
        u.email_canonical = u.email
        self.db.add(u)
        return u

    # -- catalogue --

    def catalogue(self) -> None:
        for name in AREAS:
            self.areas.append(Area(name=name, slug=name.lower().replace(" ", "-")))
        self.db.add_all(self.areas)
        for slug, (name, plural, jts) in TRADES.items():
            trade = Trade(slug=slug, name=name, plural=plural)
            self.db.add(trade)
            self.trades[slug] = trade
            self.job_types[slug] = []
            for jt_name, _lo, _hi in jts:
                jt = JobType(
                    trade=trade, name=jt_name, slug=f"{slug}-{len(self.job_types[slug]) + 1}"
                )
                self.db.add(jt)
                self.job_types[slug].append(jt)
        self.db.flush()

    # -- people --

    def admins_and_demo_accounts(self) -> None:
        for name, email, n in [
            ("Platform Admin", "admin@fundifinder.test", 990),
            ("Second Admin", "admin2@fundifinder.test", 991),
        ]:
            self.admins.append(
                self.user(name, Role.ADMIN, self.ago(600), email=email, phone=self.phone(n))
            )

    def make_fundis(self) -> None:
        plan: list[str] = []
        for slug, w in TRADE_WEIGHTS.items():
            plan += [slug] * w
        self.rng.shuffle(plan)
        for i, slug in enumerate(plan):
            established = i < 44
            if established:
                n_jobs = 5 + round(8 * (i + 1) ** -0.8)
                created = self.ago(self.rng.uniform(220, 520))
            elif i < 52:
                n_jobs = self.rng.choice([3, 4])  # joined a few months ago, still building a record
                created = self.ago(self.rng.uniform(40, 110))
            else:
                n_jobs = [1, 2, 0, 1, 0, 1, 0, 3][i - 52]
                created = self.ago(self.rng.uniform(4, 40))
            quality = min(4.9, max(3.2, self.rng.gauss(4.35, 0.35)))
            if i == 0:
                quality, n_jobs = 4.8, 45
            elif i == 1:
                quality, n_jobs = 4.6, 30
            elif i == 3:
                quality, n_jobs = 3.4, 18  # prolific but mediocre: volume is not quality
            if i in (20, 27, 33):
                n_jobs = 3  # the ring targets have thin honest histories
            name = self.name()
            email = "fundi@fundifinder.test" if i == 1 else None
            user = self.user(
                name,
                Role.FUNDI,
                created,
                email=email,
                verified=self.rng.random() < (0.97 if established else 0.8),
            )
            self.db.flush()
            trade = self.trades[slug]
            prof = FundiProfile(
                user_id=user.id,
                trade_id=trade.id,
                created_at=created,
                headline=self.rng.choice(HEADLINES[slug]),
                bio=self._bio(name, slug),
                years_experience=self.rng.randint(2, 28) if established else self.rng.randint(1, 5),
            )
            prof.areas = self.rng.sample(self.areas, self.rng.randint(2, 5))
            self.db.add(prof)
            self.db.flush()
            specs = TRADES[slug][2]
            chosen = self.rng.sample(
                list(enumerate(specs)), min(len(specs), self.rng.randint(3, 5))
            )
            services: list[FundiService] = []
            for idx, (_n, lo, hi) in chosen:
                skew = self.rng.uniform(0.85, 1.25)
                lo2 = max(100, round(lo * skew / 50) * 50)
                hi2 = max(lo2 + 50, round(hi * skew * self.rng.uniform(0.9, 1.1) / 50) * 50)
                svc = FundiService(
                    fundi_id=prof.id,
                    job_type_id=self.job_types[slug][idx].id,
                    price_min_kes=lo2,
                    price_max_kes=hi2,
                )
                self.db.add(svc)
                services.append(svc)
            self.sims.append(
                Sim(
                    prof,
                    user,
                    quality,
                    p_on_time=min(0.99, max(0.55, self.rng.gauss(0.87, 0.1))),
                    p_fast=min(0.99, max(0.5, self.rng.gauss(0.85, 0.12))),
                    delay_hours=self.rng.uniform(0.5, 9),
                    n_jobs=n_jobs,
                    area_ids=[a.id for a in prof.areas],
                    services=services,
                    created=created,
                )
            )
        self.db.flush()
        self.summary.fundis = len(self.sims)

    def _bio(self, name: str, slug: str) -> str:
        first = name.split(maxsplit=1)[0]
        trade = TRADES[slug][0].lower()
        return (
            f"{first} has worked as a {trade} across Nairobi for years. "
            "Gives a written price before starting, and says so if a job is not worth doing. "
            "(Sample profile: synthetic data.)"
        )

    def availability(self) -> None:
        for sim in self.sims:
            pid = sim.profile.id
            style = self.rng.random()
            days = (
                [0, 1, 2, 3, 4]
                if style < 0.55
                else ([0, 1, 2, 3, 4, 5] if style < 0.8 else [1, 2, 3, 4, 5])
            )
            start, end = self.rng.choice([(8, 17), (7, 18), (9, 17), (8, 16)])
            for d in days:
                self.db.add(
                    AvailabilityRule(
                        fundi_id=pid, weekday=d, start_hour=start, end_hour=13 if d == 5 else end
                    )
                )
            today = self.now.date()
            if self.rng.random() < 0.18:  # away for the coming week
                for k in range(0, 7):
                    self.db.add(
                        AvailabilityException(
                            fundi_id=pid,
                            day=today + timedelta(days=k),
                            available=False,
                            note="Away",
                        )
                    )
            elif self.rng.random() < 0.3:
                self.db.add(
                    AvailabilityException(
                        fundi_id=pid,
                        day=today + timedelta(days=self.rng.randint(1, 10)),
                        available=False,
                        note="Day off",
                    )
                )

    def verification(self) -> None:
        admin = self.admins[0]
        pending_left = 6
        for sim in self.sims:
            prof, user = sim.profile, sim.user
            slug = (
                prof.trade.slug
                if prof.trade
                else next(s for s, t in self.trades.items() if t.id == prof.trade_id)
            )
            if self.rng.random() < 0.55 and user.phone_verified_at:
                doc = IdDocument(
                    fundi_id=prof.id,
                    storage_name=self._store("private", placeholder_id_image(user.name)),
                    status=IdStatus.APPROVED,
                    created_at=sim.created + timedelta(days=2),
                    reviewed_by_id=admin.id,
                    reviewed_at=sim.created + timedelta(days=3),
                )
                self.db.add(doc)
                self.db.add(
                    AuditLog(
                        actor_id=admin.id,
                        action="id.approved",
                        target_type="fundi",
                        target_id=prof.id,
                        detail="Demo seed",
                        created_at=doc.reviewed_at,
                    )
                )
                self.summary.id_documents += 1
            elif pending_left and user.phone_verified_at and sim.n_jobs >= 3:
                pending_left -= 1
                self.db.add(
                    IdDocument(
                        fundi_id=prof.id,
                        storage_name=self._store("private", placeholder_id_image(user.name)),
                        created_at=self.ago(self.rng.uniform(0.2, 3)),
                    )
                )
                self.summary.id_documents += 1
            if self.rng.random() < 0.38 and user.phone_verified_at:
                for k in range(self.rng.randint(2, 3)):
                    self.db.add(
                        Reference(
                            fundi_id=prof.id,
                            name=self.name(),
                            phone=self.phone(),
                            token=f"seed-{prof.id}-{k}-{self.rng.getrandbits(48):012x}",
                            status=RefStatus.CONFIRMED,
                            created_at=self.ago(60),
                            confirmed_at=self.ago(58),
                        )
                    )
            elif self.rng.random() < 0.1:
                self.db.add(
                    Reference(
                        fundi_id=prof.id,
                        name=self.name(),
                        phone=self.phone(),
                        token=f"seed-{prof.id}-p-{self.rng.getrandbits(48):012x}",
                        created_at=self.ago(3),
                    )
                )
            if self.rng.random() < 0.55:
                n_photos = self.rng.choice([1, 2, 3, 3, 4, 5])
                for k in range(n_photos):
                    variant = (prof.id + k) % 4
                    key = (slug, variant)
                    if key not in self._photo_cache:
                        self._photo_cache[key] = placeholder_work_image(slug, variant)
                    self.db.add(
                        PortfolioPhoto(
                            fundi_id=prof.id,
                            storage_name=self._store("public", self._photo_cache[key]),
                            caption=f"Sample {TRADES[slug][0].lower()} work {k + 1}",
                            created_at=self.ago(90),
                        )
                    )

    def _store(self, zone: str, data: bytes) -> str:
        return self.storage.save(zone, data, "png")

    # -- jobs --

    def make_customers(self, n: int) -> None:
        for _ in range(n):
            created = self.ago(self.rng.uniform(8, 540))
            self.customers.append(self.user(self.name(), Role.CUSTOMER, created))
        self.customers.sort(key=lambda u: u.created_at)
        self.db.flush()
        self.summary.customers = n
        self._customer_times = [u.created_at for u in self.customers]

    def pick_customer(self, when: datetime) -> User:
        cut = bisect_left(self._customer_times, when - timedelta(days=2))
        pool = self.customers[: max(cut, 1)]
        return self.rng.choice(pool)

    def sample_rating(self, quality: float) -> int:
        if self.rng.random() < 0.04:  # everyone has a bad day sometimes
            return self.rng.choice([1, 2, 2, 3])
        weights = [pow(2.718, -((r - quality) ** 2) / (2 * 0.8**2)) for r in range(1, 6)]
        return self.rng.choices([1, 2, 3, 4, 5], weights)[0]

    def price_for(self, svc: FundiService) -> int:
        frac = min(1.0, max(0.0, self.rng.betavariate(2, 2.4)))
        return round((svc.price_min_kes + frac * (svc.price_max_kes - svc.price_min_kes)) / 50) * 50

    def _request_and_quote(
        self,
        sim: Sim,
        customer: User,
        svc: FundiService,
        scheduled: datetime,
        price: int,
        answered: bool = True,
    ) -> tuple[QuoteRequest, Quote]:
        created = scheduled - timedelta(days=self.rng.uniform(1, 6))
        created = max(created, customer.created_at + timedelta(hours=1))
        area_id = self.rng.choice(sim.area_ids)
        req = QuoteRequest(
            customer_id=customer.id,
            fundi_id=sim.profile.id,
            job_type_id=svc.job_type_id,
            area_id=area_id,
            description=self._describe(svc),
            preferred_time=scheduled,
            created_at=created,
            status=RequestStatus.ACCEPTED,
        )
        self.db.add(req)
        self.db.flush()
        delay = (
            self.rng.uniform(0.2, 1.0) * sim.delay_hours
            if self.rng.random() < sim.p_fast
            else self.rng.uniform(26, 90)
        )
        req.first_response_at = max(
            created + timedelta(minutes=5),
            min(created + timedelta(hours=delay), scheduled - timedelta(minutes=30)),
        )
        quote = Quote(
            request_id=req.id,
            amount_kes=price,
            message="Price includes labour. Materials as discussed.",
            proposed_start=scheduled,
            status=QuoteStatus.ACCEPTED,
            created_at=req.first_response_at,
        )
        self.db.add(quote)
        self.db.flush()
        return req, quote

    def _describe(self, svc: FundiService) -> str:
        jt = next(j for js in self.job_types.values() for j in js if j.id == svc.job_type_id)
        return f"{jt.name}. Please come and look first, then give me a price. I am home in the mornings."

    def add_job(
        self,
        sim: Sim,
        customer: User,
        status: str,
        scheduled: datetime,
        svc: FundiService | None = None,
        price: int | None = None,
        on_time: bool | None = None,
    ) -> Job:
        svc = svc or self.rng.choice(sim.services)
        price = price or self.price_for(svc)
        req, quote = self._request_and_quote(sim, customer, svc, scheduled, price)
        job = Job(
            request_id=req.id,
            quote_id=quote.id,
            customer_id=customer.id,
            fundi_id=sim.profile.id,
            job_type_id=svc.job_type_id,
            area_id=req.area_id,
            price_kes=price,
            scheduled_for=scheduled,
            status=JobStatus.BOOKED,
            created_at=quote.created_at + timedelta(hours=self.rng.uniform(0.5, 20)),
            code_salt=f"{self.rng.getrandbits(128):032x}",
        )
        started = None
        if status in ("completed", "awaiting_confirmation", "in_progress"):
            punctual = self.rng.random() < sim.p_on_time if on_time is None else on_time
            started = scheduled + (
                timedelta(minutes=-self.rng.randint(0, 10))
                if punctual
                else timedelta(minutes=self.rng.randint(25, 140))
            )
            job.started_at = started
        if status in ("completed", "awaiting_confirmation"):
            assert started is not None
            job.marked_complete_at = started + timedelta(hours=self.rng.uniform(1, 5))
        if status == "completed":
            assert job.marked_complete_at is not None
            job.completed_at = job.marked_complete_at + timedelta(hours=self.rng.uniform(0.1, 3))
            job.code_used_at = job.completed_at
            job.code_confirmed_by = "fundi" if self.rng.random() < 0.2 else "customer"
        if status == "cancelled":
            job.cancelled_by = "customer" if self.rng.random() < 0.72 else "fundi"
            job.cancel_reason = (
                "Plans changed" if job.cancelled_by == "customer" else "Double booked"
            )
        job.status = status
        self.db.add(job)
        self.summary.jobs += 1
        if status == "completed":
            self.summary.completed_jobs += 1
        return job

    def add_review(
        self, job: Job, rating: int, *, ring: bool = False, delay_days: float | None = None
    ) -> Review:
        assert job.completed_at is not None
        self.db.flush()
        created = min(
            job.completed_at
            + timedelta(days=delay_days if delay_days is not None else self.rng.uniform(0.1, 5)),
            self.now,
        )
        if ring:
            body = self.rng.choice(RING_TEXT)
        elif rating >= 4:
            body = f"{self.rng.choice(POS_OPEN)} {self.rng.choice(POS_CLOSE)}".strip()
        elif rating == 3:
            body = self.rng.choice(MID)
        else:
            body = self.rng.choice(NEG)
        review = Review(
            job_id=job.id,
            customer_id=job.customer_id,
            fundi_id=job.fundi_id,
            rating=rating,
            body=body,
            created_at=created,
        )
        if not ring and self.rng.random() < 0.35:
            review.reply_body = self.rng.choice(REPLIES_POS if rating >= 4 else REPLIES_NEG)
            review.reply_at = created + timedelta(days=self.rng.uniform(0.2, 3))
        self.db.add(review)
        self.summary.reviews += 1
        if not ring:
            self.honest_reviews.append(review)
        return review

    def bulk_jobs(self) -> None:
        for i, sim in enumerate(self.sims):
            span = max(10.0, (self.now - sim.created).days - 10)
            for k in range(sim.n_jobs):
                days_back = 3 + (self.rng.random() ** 1.4) * span
                scheduled = (self.now - timedelta(days=days_back)).replace(
                    hour=self.rng.randint(8, 16),
                    minute=self.rng.choice([0, 30]),
                    second=0,
                    microsecond=0,
                )
                scheduled = max(scheduled, sim.created + timedelta(days=3))
                customer = self.pick_customer(scheduled)
                job = self.add_job(sim, customer, "completed", scheduled)
                # newcomers: the first two are planted 5-star reviews for the cold-start demo
                if i == 52 or i == 53:
                    rating = 5
                elif self.rng.random() < 0.88:
                    rating = self.sample_rating(sim.quality)
                else:
                    continue
                self.add_review(job, rating)
        self.db.flush()

    def unfinished_jobs(self, total: int) -> None:
        established = [s for s in self.sims if s.n_jobs >= 3]
        for _ in range(total):
            sim = self.rng.choice(established)
            scheduled = (self.now - timedelta(days=self.rng.uniform(2, 380))).replace(
                hour=self.rng.randint(8, 16), minute=0, second=0, microsecond=0
            )
            scheduled = max(scheduled, sim.created + timedelta(days=3))
            roll = self.rng.random()
            status = "cancelled" if roll < 0.8 else "no_show"
            customer = self.pick_customer(scheduled)
            job = self.add_job(
                sim, customer, "cancelled" if status == "cancelled" else "booked", scheduled
            )
            if status == "no_show":
                job.status = JobStatus.NO_SHOW

    # -- planted abuse rings --

    def rings(self) -> None:
        targets = {"A": self.sims[20], "B": self.sims[27], "C": self.sims[33]}
        # Ring A: six accounts created the same day, consecutive SIM block, all 5 stars
        day = self.ago(40).replace(hour=9, minute=0, second=0, microsecond=0)
        ring_a = [
            self.user(
                self.name(),
                Role.CUSTOMER,
                day + timedelta(minutes=7 * k),
                phone=self.phone(900 + k),
            )
            for k in range(6)
        ]
        # Ring B: five accounts, same day, same sign-up network, scattered numbers
        day_b = self.ago(75).replace(hour=14, minute=0, second=0, microsecond=0)
        net_b = f"{self.rng.getrandbits(64):016x}"
        ring_b = [
            self.user(self.name(), Role.CUSTOMER, day_b + timedelta(minutes=11 * k), ip=net_b)
            for k in range(5)
        ]
        # Ring C: the fundi's own alternate accounts, same network and SIM block
        z = targets["C"]
        z.user.signup_ip_hash = f"{self.rng.getrandbits(64):016x}"
        z.user.phone = self.phone(880)
        alts = [
            self.user(
                self.name(),
                Role.CUSTOMER,
                z.user.created_at + timedelta(hours=1 + k),
                ip=z.user.signup_ip_hash,
                phone=self.phone(881 + k),
            )
            for k in range(3)
        ]
        self.db.flush()
        for label, accounts, sim, start in (
            ("A", ring_a, targets["A"], self.ago(38)),
            ("B", ring_b, targets["B"], self.ago(72)),
            ("C", alts, targets["C"], self.ago(95)),
        ):
            if label == "C":
                start = max(start, z.user.created_at + timedelta(days=4))
            for k, acct in enumerate(accounts):
                scheduled = start + timedelta(hours=9 * k + 2)
                svc = sim.services[0]
                job = self.add_job(
                    sim,
                    acct,
                    "completed",
                    scheduled,
                    svc=svc,
                    price=svc.price_min_kes,
                    on_time=True,
                )
                self.db.flush()
                self.add_review(job, 5, ring=True, delay_days=0.1 + 0.2 * k)
            self.summary.rings[label] = len(accounts)
        self.db.flush()

    # -- the demo cast --

    def demo_cast(self) -> None:
        demo_fundi = self.sims[1]
        cust = self.user(
            "Amina Demo-Customer",
            Role.CUSTOMER,
            self.ago(200),
            email="customer@fundifinder.test",
            phone=self.phone(992),
        )
        self.db.flush()
        others = [s for s in self.sims[2:30] if s.profile.trade_id != demo_fundi.profile.trade_id]
        # live jobs with the demo fundi
        self.add_job(demo_fundi, cust, "booked", self.now + timedelta(days=3, hours=2))
        awaiting = self.add_job(
            demo_fundi, cust, "awaiting_confirmation", self.now - timedelta(hours=5)
        )
        # a completed job waiting for its review, and one old reviewed job
        waiting = self.add_job(others[0], cust, "completed", self.ago(9))
        old = self.add_job(others[1], cust, "completed", self.ago(70))
        self.db.flush()
        rev = self.add_review(old, 5, delay_days=1)
        rev.reply_body, rev.reply_at = (
            "Thank you, glad it worked out.",
            rev.created_at + timedelta(days=1),
        )
        # a request that has a quote waiting to be accepted, and one still open
        for sim, quoted in ((others[2], True), (others[3], False)):
            svc = self.rng.choice(sim.services)
            req = QuoteRequest(
                customer_id=cust.id,
                fundi_id=sim.profile.id,
                job_type_id=svc.job_type_id,
                area_id=sim.area_ids[0],
                description=self._describe(svc),
                preferred_time=self.now + timedelta(days=4),
                created_at=self.ago(0.5),
                status=RequestStatus.QUOTED if quoted else RequestStatus.OPEN,
            )
            self.db.add(req)
            self.db.flush()
            if quoted:
                req.first_response_at = self.ago(0.2)
                self.db.add(
                    Quote(
                        request_id=req.id,
                        amount_kes=self.price_for(svc),
                        message="I can start on the day you asked. Price includes labour.",
                        proposed_start=self.now + timedelta(days=4, hours=1),
                        created_at=self.ago(0.2),
                    )
                )
        # open requests in the demo fundi's inbox
        for k in range(3):
            svc = self.rng.choice(demo_fundi.services)
            c = self.rng.choice(self.customers[:80])
            self.db.add(
                QuoteRequest(
                    customer_id=c.id,
                    fundi_id=demo_fundi.profile.id,
                    job_type_id=svc.job_type_id,
                    area_id=demo_fundi.area_ids[0],
                    description=self._describe(svc),
                    preferred_time=self.now + timedelta(days=2 + k),
                    created_at=self.ago(0.1 * (k + 1)),
                )
            )
        # a handful of in-flight jobs elsewhere so the admin and fundi views have live data
        for sim in self.rng.sample(
            [s for s in self.sims if s.n_jobs >= 6 and s is not demo_fundi], 6
        ):
            c = self.pick_customer(self.now)
            self.add_job(
                sim, c, "booked", self.now + timedelta(days=self.rng.randint(1, 6), hours=3)
            )
        for sim in self.rng.sample(
            [s for s in self.sims if s.n_jobs >= 6 and s is not demo_fundi], 3
        ):
            c = self.pick_customer(self.now)
            self.add_job(
                sim, c, "awaiting_confirmation", self.now - timedelta(hours=self.rng.randint(3, 20))
            )
        self.db.flush()
        self._waiting_review_job = waiting
        self._awaiting_job = awaiting

    # -- moderation backlog --

    def moderation(self) -> None:
        admin1, admin2 = self.admins
        honest = [r for r in self.honest_reviews if r.rating >= 4 and r.body]
        negative = [r for r in self.honest_reviews if r.rating <= 2]
        pool = self.rng.sample(honest, 3)
        reporters = self.rng.sample(self.customers, 3)
        for review, reporter, reason in zip(
            pool, reporters, ["abusive", "personal", "other"], strict=True
        ):
            self.db.add(
                Report(
                    reporter_id=reporter.id,
                    review_id=review.id,
                    reason=reason,
                    detail="Demo report for the moderation queue.",
                    created_at=self.ago(self.rng.uniform(0.2, 4)),
                )
            )
        if negative:
            neg = negative[0]
            fundi_user = self.db.get(FundiProfile, neg.fundi_id).user  # type: ignore[union-attr]
            self.db.add(
                Report(
                    reporter_id=fundi_user.id,
                    review_id=neg.id,
                    reason="not_customer",
                    detail="I have no record of this customer. Please check.",
                    created_at=self.ago(1),
                )
            )
        # an upheld takedown with an appeal waiting for the *other* admin
        if len(honest) > 8 and negative:
            target = negative[-1]
            report = Report(
                reporter_id=self.db.get(FundiProfile, target.fundi_id).user_id,  # type: ignore[union-attr]
                review_id=target.id,
                reason="abusive",
                detail="Contains insults.",
                status=ReportStatus.UPHELD,
                created_at=self.ago(6),
                resolved_by_id=admin1.id,
                resolved_at=self.ago(5),
                resolution_note="Insulting language, removed.",
                appeal_status=AppealStatus.PENDING,
                appeal_text="I only described what happened. The word was about the work, not the person.",
                appeal_at=self.ago(3),
            )
            target.status = "removed"
            self.db.add(report)
            self.db.add(
                AuditLog(
                    actor_id=admin1.id,
                    action="report.upheld",
                    target_type="report",
                    detail="Demo seed",
                    created_at=report.resolved_at,
                )
            )
        dismissed = honest[-1]
        self.db.add(
            Report(
                reporter_id=self.customers[5].id,
                review_id=dismissed.id,
                reason="fake",
                detail="Seems too positive.",
                status=ReportStatus.DISMISSED,
                created_at=self.ago(9),
                resolved_by_id=admin2.id,
                resolved_at=self.ago(8),
                resolution_note="Reviewer has a confirmed job with this fundi.",
            )
        )
        self.db.add(
            AuditLog(
                actor_id=admin2.id,
                action="report.dismissed",
                target_type="report",
                detail="Demo seed",
                created_at=self.ago(8),
            )
        )
        self.summary.reports = 6


def seed_demo(app: Flask, rng_seed: int = 42) -> SeedSummary:
    """Populate an empty database. Call inside an app context."""
    db = get_db()
    if db.scalar(select(func.count()).select_from(User)):
        raise SeedError("The database already has data. Use `flask seed --reset` to start again.")
    s = Seeder(app, rng_seed)
    s.catalogue()
    s.admins_and_demo_accounts()
    s.make_fundis()
    s.make_customers(250)
    s.availability()
    s.verification()
    s.bulk_jobs()
    s.unfinished_jobs(40)
    s.rings()
    s.demo_cast()
    db.flush()
    s.moderation()
    db.commit()
    flags = abuse_scan.scan(db, s.admins[0])
    db.commit()
    s.summary.abuse_flags = len(flags)
    return s.summary


def demo_code_for(app: Flask, job: Job) -> str:
    return derive_code(str(app.config["SECRET_KEY"]), job.code_salt)


def open_flags(db: Session) -> list[AbuseFlag]:
    return list(db.scalars(select(AbuseFlag)))
