"""The browser demo's engine, run under CPython against the real domain modules.

The demo has no second copy of the rules, so these tests check the glue and the story the demo
tells: the review gate, the code outcomes, the clock, saved state, and that the engine imports
without Flask or Pillow (the browser has neither).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fundifinder import clock

ROOT = Path(__file__).resolve().parent.parent
ENGINE_PATH = ROOT / "demo" / "engine" / "demo_engine.py"
FIXED_NOW = "2026-06-15T10:00:00"


def load_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_engine_under_test", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def eng() -> Iterator[ModuleType]:
    module = load_engine()
    module.init(None, FIXED_NOW)
    yield module
    clock.set_frozen(None)


def call(eng: ModuleType, name: str, **args: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(eng.call(name, json.dumps(args)))
    return out


def data(eng: ModuleType, name: str, **args: Any) -> Any:
    out = call(eng, name, **args)
    assert out["ok"], out
    return out["data"]


def item_for(eng: ModuleType, job_type: str) -> dict[str, Any]:
    items = data(eng, "bookings")["items"]
    return next(i for i in items if i["job_type"] == job_type)


def awaiting_job(eng: ModuleType) -> dict[str, Any]:
    job = item_for(eng, "Unblock a sink or drain")["job"]
    assert job["status"] == "awaiting_confirmation"
    return job


def test_engine_imports_without_flask_or_pillow() -> None:
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('e', {str(ENGINE_PATH)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "bad = [n for n in ('flask', 'werkzeug', 'PIL') if n in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_seed_is_deterministic_and_invented(eng: ModuleType) -> None:
    first = [c["name"] for c in data(eng, "search")["results"]]
    other = load_engine()
    other.init(None, FIXED_NOW)
    assert [c["name"] for c in data(other, "search")["results"]] == first
    platform = data(eng, "catalog")["platform"]
    assert platform["fundis"] == 15
    assert platform["reviews"] > 150


def test_search_filters_by_trade_area_and_badges(eng: ModuleType) -> None:
    cat = data(eng, "catalog")
    plumber = next(t["id"] for t in cat["trades"] if t["name"] == "Plumber")
    kilimani = next(a["id"] for a in cat["areas"] if a["name"] == "Kilimani")
    hits = data(eng, "search", trade_id=plumber, area_id=kilimani)["results"]
    assert hits
    assert {h["trade"] for h in hits} == {"Plumber"}
    assert all("Kilimani" in h["areas"] for h in hits)
    verified = data(eng, "search", verified_only=True)["results"]
    assert verified
    assert all(len(h["badges"]) >= 2 for h in verified)
    week = data(eng, "search", available_week=True)["results"]
    assert len(week) < len(data(eng, "search")["results"])  # three fundis are away this week
    cheap = data(eng, "search", max_price=600)["results"]
    assert cheap
    assert all(c["price_min"] <= 600 for c in cheap)


def test_ranking_reserves_every_fourth_slot_and_one_review_does_not_win(eng: ModuleType) -> None:
    results = data(eng, "search")["results"]
    assert results[3]["boosted"] is True
    one_review = next(r for r in results if r["name"] == "Peter Njoroge")
    established = [r for r in results if not r["is_new"]]
    assert one_review["rating"] == 5.0
    assert one_review["review_count"] == 1
    assert one_review["score"] < min(r["score"] for r in established[:5])


def test_review_is_refused_until_the_code_confirms_the_job(eng: ModuleType) -> None:
    job = awaiting_job(eng)
    refused = call(eng, "review", job_id=job["id"], rating=5, body="Great")
    assert refused["ok"] is False
    assert refused["kind"] == "rule"
    assert "confirmed with your completion code" in refused["error"]
    # The unconfirmed job adds nothing to the fundi's numbers.
    joseph = next(r for r in data(eng, "search")["results"] if r["name"] == "Joseph Mwangi")
    before = joseph["review_count"]
    assert data(eng, "profile", id=joseph["id"])["record"]["reviews_without_confirmed_job"] == 0
    wrong = data(eng, "confirm", job_id=job["id"], actor="customer", code="000000")
    assert wrong["result"] in ("wrong", "ok")
    again = next(r for r in data(eng, "search")["results"] if r["name"] == "Joseph Mwangi")
    assert again["review_count"] == before


def test_wrong_codes_lock_then_the_clock_unlocks(eng: ModuleType) -> None:
    job = awaiting_job(eng)
    real = job["code"]
    wrong = "111111" if real != "111111" else "222222"
    seen = [data(eng, "confirm", job_id=job["id"], actor="customer", code=wrong) for _ in range(5)]
    assert [s["result"] for s in seen[:4]] == ["wrong"] * 4
    assert seen[0]["attempts_left"] == 4
    assert seen[4]["result"] == "locked"
    # The right code is refused while locked.
    locked = data(eng, "confirm", job_id=job["id"], actor="customer", code=real)
    assert locked["result"] == "locked"
    data(eng, "advance_clock", hours=1.1)
    ok = data(eng, "confirm", job_id=job["id"], actor="customer", code=real)
    assert ok["result"] == "ok"


def test_ten_wrong_codes_disable_the_code_until_an_admin_reissues_it(eng: ModuleType) -> None:
    job = awaiting_job(eng)
    real = job["code"]
    wrong = "111111" if real != "111111" else "222222"
    results = []
    for _ in range(10):
        results.append(
            data(eng, "confirm", job_id=job["id"], actor="customer", code=wrong)["result"]
        )
        data(eng, "advance_clock", hours=1.1)  # step over each one-hour lock
    assert results[-1] == "invalidated"
    after = awaiting_job(eng)
    assert after["code_invalidated"] is True
    assert after["code"] is None
    assert (
        data(eng, "confirm", job_id=job["id"], actor="customer", code=real)["result"]
        == "invalidated"
    )
    data(eng, "reissue", job_id=job["id"])
    fresh = awaiting_job(eng)
    assert fresh["code"] and fresh["code"] != real
    assert (
        data(eng, "confirm", job_id=job["id"], actor="fundi", code=fresh["code"])["result"] == "ok"
    )


def test_full_booking_to_review_changes_the_aggregate(eng: ModuleType) -> None:
    cat = data(eng, "catalog")
    brian = next(r for r in data(eng, "search")["results"] if r["name"] == "Brian Otieno")
    prof = data(eng, "profile", id=brian["id"])
    svc = prof["services"][0]
    area = prof["area_options"][0]
    req = data(
        eng,
        "request_quote",
        fundi_id=brian["id"],
        job_type_id=svc["job_type_id"],
        area_id=area["id"],
        description="Replace two cracked sockets in the hallway.",
        preferred_time="2026-06-17T09:00:00+03:00",
    )
    data(
        eng,
        "send_quote",
        request_id=req["request_id"],
        amount="1,500",
        start="2026-06-17T09:00:00+03:00",
        message="",
    )
    quote = item_for(eng, svc["name"])
    assert quote["quote"]["amount"] == 1500
    job = data(eng, "accept_quote", quote_id=quote["quote"]["id"])
    job_id = job["job_id"]
    # Out of order is refused by the real state machine.
    early = call(eng, "job_event", job_id=job_id, actor="fundi", event="mark_complete")
    assert early["ok"] is False
    assert "Cannot mark_complete a job that is booked" in early["error"]
    data(eng, "job_event", job_id=job_id, actor="fundi", event="start")
    data(eng, "job_event", job_id=job_id, actor="fundi", event="mark_complete")
    code = next(i for i in data(eng, "bookings")["items"] if i["job"] and i["job"]["id"] == job_id)[
        "job"
    ]["code"]
    # The fundi cannot confirm without the code, and the review is still refused.
    assert call(eng, "review", job_id=job_id, rating=5, body="")["ok"] is False
    assert data(eng, "confirm", job_id=job_id, actor="fundi", code="000000")["result"] == "wrong"
    assert data(eng, "confirm", job_id=job_id, actor="customer", code=code)["result"] == "ok"
    done = data(eng, "review", job_id=job_id, rating=5, body="Tidy and on time.")
    assert done["after"]["review_count"] == done["before"]["review_count"] + 1
    # Used code, second try
    reuse = data(eng, "probe_reuse", job_id=job_id, code=code)
    assert reuse["result"] == "used"
    second = call(eng, "review", job_id=job_id, rating=1, body="again")
    assert second["ok"] is False
    assert "already reviewed" in second["error"]
    assert cat["limits"]["review_window_days"] == 30


def test_review_window_closes_after_thirty_days(eng: ModuleType) -> None:
    job = item_for(eng, "Make curtains, per window")["job"]
    assert job["status"] == "completed"
    assert job["can_review"] is True
    data(eng, "advance_clock", hours=24 * 31)
    refused = call(eng, "review", job_id=job["id"], rating=4, body="")
    assert refused["ok"] is False
    assert "30-day window" in refused["error"]


def test_saved_state_round_trips_and_resumes_the_demo_clock(eng: ModuleType) -> None:
    job = awaiting_job(eng)
    data(eng, "confirm", job_id=job["id"], actor="customer", code="000000")
    data(eng, "advance_clock", hours=2)
    saved = eng.export_state()
    before = data(eng, "bookings")
    other = load_engine()
    assert json.loads(other.init(saved, "2026-06-20T08:00:00"))["restored"] is True
    after = data(other, "bookings")
    assert after["items"] == before["items"]
    assert after["now"] == before["now"]  # the demo clock resumes where it was saved


def test_a_stale_or_broken_save_falls_back_to_fresh_data(eng: ModuleType) -> None:
    other = load_engine()
    assert json.loads(other.init("not json"))["restored"] is False
    assert data(other, "catalog")["platform"]["fundis"] == 15
    assert json.loads(other.init(json.dumps({"v": 0, "sql": "x"})))["restored"] is False


def test_unknown_input_is_reported_not_raised(eng: ModuleType) -> None:
    assert call(eng, "profile", id=99999)["ok"] is False
    assert call(eng, "confirm", job_id=99999, actor="customer", code="1")["ok"] is False
    bad = call(
        eng,
        "request_quote",
        fundi_id=1,
        job_type_id=1,
        area_id=1,
        description="short",
        preferred_time="x",
    )
    assert bad["ok"] is False
