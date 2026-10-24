"""QR self check-in + PHG member-code tests.

Covers the fully automatic member flow (wall poster -> /checkin/<token>
-> POST /api/checkin/scan) and the member-code rules: every member holds
a unique PHG code, new members are auto-assigned the next sequential
number from a monotonic counter (never reused after deletion), and any
code-less member is backfilled.
"""
import re
from datetime import date, timedelta

import pytest

import main
import member_codes
import models
from database import SessionLocal
from google_auth import app_base_url

CODE_RE = re.compile(r"^PHG-\d{3}$")


@pytest.fixture(autouse=True)
def _clear_scan_rate_limit():
    main._scan_attempts.clear()
    yield
    main._scan_attempts.clear()


def _token(auth) -> str:
    r = auth.get("/api/gym/checkin-link")
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ------------------------------------------------------- check-in link ----
def test_checkin_link_requires_login(client):
    assert client.get("/api/gym/checkin-link").status_code == 401
    assert client.post("/api/gym/checkin-link/reset").status_code == 401


def test_checkin_link_shape(auth):
    r = auth.get("/api/gym/checkin-link")
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["url"] == f"{app_base_url()}/checkin/{body['token']}"
    assert body["gym_name"] == "PowerHouse Gym"
    # Stable across calls: the same poster keeps working.
    assert auth.get("/api/gym/checkin-link").json()["token"] == body["token"]


# -------------------------------------------------------- member page ----
def test_member_page_valid_token(client, auth):
    token = _token(auth)
    r = client.get(f"/checkin/{token}")
    assert r.status_code == 200
    assert "PowerHouse Gym" in r.text
    assert "Your member code" in r.text
    assert token in r.text  # the page posts back with this token


def test_member_page_unknown_token_is_friendly_404(client, auth):
    _token(auth)  # a real token exists; this one is not it
    r = client.get("/checkin/definitely-not-the-token")
    assert r.status_code == 404
    assert "not valid" in r.text.lower()
    assert "front desk" in r.text.lower()


# ------------------------------------------------------------- scanning ----
def test_scan_happy_path_records_member_time_and_source(client, auth):
    token = _token(auth)
    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["already_checked_in"] is False
    assert body["member_name"] == "Rohit A."
    assert body["member_code"] == "PHG-001"
    assert body["gym_name"] == "PowerHouse Gym"
    assert body["time"]  # entry time recorded
    assert body["date"] == date.today().isoformat()

    with SessionLocal() as db:
        member = db.query(models.Member).filter(models.Member.code == "PHG-001").first()
        rows = db.query(models.CheckIn).filter(
            models.CheckIn.member_id == member.id,
            models.CheckIn.date == date.today(),
        ).all()
        assert len(rows) == 1
        assert rows[0].source == "qr"
        assert rows[0].time_str == body["time"]


def test_scan_updates_owner_views(client, auth):
    token = _token(auth)
    before_stats = auth.get("/api/stats").json()
    before_red = auth.get("/api/red-list").json()["members"]
    assert before_stats["checked_in_today"] == 5
    assert len(before_red) == 32

    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-001"})
    assert r.status_code == 200

    after_stats = auth.get("/api/stats").json()
    after_red = auth.get("/api/red-list").json()["members"]
    assert after_stats["checked_in_today"] == 6
    assert after_stats["red_list_count"] == 31
    assert len(after_red) == 31
    assert "PHG-001" not in [m["code"] for m in after_red]

    today = auth.get("/api/checkins/today").json()["checkins"]
    entry = next(c for c in today if c["code"] == "PHG-001")
    assert entry["name"] == "Rohit A."
    assert entry["source"] == "qr"


def test_scan_is_idempotent_same_day(client, auth):
    token = _token(auth)
    first = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-002"})
    assert first.status_code == 200
    second = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-002"})
    assert second.status_code == 200
    body = second.json()
    assert body["already_checked_in"] is True
    assert body["time"] == first.json()["time"]
    assert f"Already checked in at {first.json()['time']}" in body["message"]

    today = auth.get("/api/checkins/today").json()["checkins"]
    assert len([c for c in today if c["code"] == "PHG-002"]) == 1


def test_scan_accepts_lowercase_code(client, auth):
    token = _token(auth)
    r = client.post("/api/checkin/scan", json={"token": token, "code": "phg-003"})
    assert r.status_code == 200
    assert r.json()["member_code"] == "PHG-003"


def test_scan_wrong_code_friendly_error_and_no_row(client, auth):
    token = _token(auth)
    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-999"})
    assert r.status_code == 404
    assert "could not find that member code" in r.json()["detail"].lower()
    assert auth.get("/api/stats").json()["checked_in_today"] == 5


def test_scan_wrong_token_404(client, auth):
    _token(auth)
    r = client.post("/api/checkin/scan", json={"token": "bogus-token", "code": "PHG-001"})
    assert r.status_code == 404
    assert auth.get("/api/stats").json()["checked_in_today"] == 5


def test_scan_rate_limited(client, auth):
    token = _token(auth)
    for _ in range(main.SCAN_LIMIT):
        r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-999"})
        assert r.status_code == 404
    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-999"})
    assert r.status_code == 429


def test_token_reset_retires_old_poster(client, auth):
    old = _token(auth)
    r = auth.post("/api/gym/checkin-link/reset")
    assert r.status_code == 200
    new = r.json()["token"]
    assert new and new != old

    assert client.get(f"/checkin/{old}").status_code == 404
    r = client.post("/api/checkin/scan", json={"token": old, "code": "PHG-001"})
    assert r.status_code == 404

    assert client.get(f"/checkin/{new}").status_code == 200
    r = client.post("/api/checkin/scan", json={"token": new, "code": "PHG-001"})
    assert r.status_code == 200


# ---------------------------------------------------------- member codes ----
def test_all_seed_members_have_unique_phg_codes(auth):
    members = auth.get("/api/members").json()["members"]
    assert len(members) == 48
    codes = [m["code"] for m in members]
    assert all(CODE_RE.match(c) for c in codes)
    assert len(set(codes)) == 48


def test_create_member_auto_assigns_next_code(auth):
    r = auth.post("/api/members", json={
        "name": "New Member", "phone": "9876543211", "plan": "Monthly",
    })
    assert r.status_code == 200, r.text
    assert r.json()["member"]["code"] == "PHG-049"
    assert "PHG-049" in r.json()["message"]

    r = auth.post("/api/members", json={
        "name": "Second Member", "phone": "9876543212", "plan": "Yearly",
    })
    assert r.status_code == 200
    assert r.json()["member"]["code"] == "PHG-050"

    codes = [m["code"] for m in auth.get("/api/members").json()["members"]]
    assert "PHG-049" in codes and "PHG-050" in codes
    assert len(codes) == len(set(codes)) == 50


def test_create_member_ignores_client_supplied_code(auth):
    r = auth.post("/api/members", json={
        "name": "Sneaky Member", "phone": "9876543213", "plan": "Monthly",
        "code": "PHG-999",
    })
    assert r.status_code == 200
    assert r.json()["member"]["code"] == "PHG-049"
    codes = [m["code"] for m in auth.get("/api/members").json()["members"]]
    assert "PHG-999" not in codes


def test_member_code_never_reused_after_delete(auth):
    r = auth.post("/api/members", json={
        "name": "Short Lived", "phone": "9876543214", "plan": "Monthly",
    })
    assert r.json()["member"]["code"] == "PHG-049"

    with SessionLocal() as db:
        m = db.query(models.Member).filter(models.Member.code == "PHG-049").first()
        db.delete(m)
        db.commit()

    r = auth.post("/api/members", json={
        "name": "Replacement", "phone": "9876543215", "plan": "Monthly",
    })
    assert r.status_code == 200
    assert r.json()["member"]["code"] == "PHG-050"  # 049 is gone forever


def test_create_member_validation(auth):
    r = auth.post("/api/members", json={
        "name": "Bad Phone", "phone": "123", "plan": "Monthly",
    })
    assert r.status_code == 400
    assert "phone" in r.json()["fields"]

    r = auth.post("/api/members", json={
        "name": "Bad Plan", "phone": "9876543216", "plan": "Weekly",
    })
    assert r.status_code == 400
    assert "plan" in r.json()["fields"]


def test_ensure_member_codes_backfills_missing(auth):
    with SessionLocal() as db:
        demo = db.query(models.User).filter(
            models.User.email == "owner@powerhousegym.in").first()
        db.add(models.Member(
            user_id=demo.id,
            code="", name="No Code Member", phone="9876543217",
            plan="Monthly", expiry_date=date.today() + timedelta(days=30),
        ))
        db.commit()

    with SessionLocal() as db:
        fixed = member_codes.ensure_member_codes(db)
    assert fixed == 1

    members = auth.get("/api/members").json()["members"]
    fixed_member = next(m for m in members if m["name"] == "No Code Member")
    assert fixed_member["code"] == "PHG-049"
    codes = [m["code"] for m in members]
    assert all(CODE_RE.match(c) for c in codes)
    assert len(codes) == len(set(codes))


def test_new_member_can_scan_in_immediately(client, auth):
    token = _token(auth)
    r = auth.post("/api/members", json={
        "name": "Fresh Joiner", "phone": "9876543218", "plan": "Quarterly",
    })
    code = r.json()["member"]["code"]
    r = client.post("/api/checkin/scan", json={"token": token, "code": code})
    assert r.status_code == 200
    assert r.json()["member_name"] == "Fresh Joiner"


def test_joining_flow_end_to_end(client, auth):
    """Part 1 of the owner's spec, whole chain: a new member joins -> the
    SYSTEM hands out the next PHG number by itself (no code field sent, no
    phone needed) -> the member scans the wall QR -> the entry lands on
    the owner's screens by itself, tagged as a QR scan."""
    members = auth.get("/api/members").json()["members"]
    assert len(members) == 48

    r = auth.post("/api/members", json={"name": "Walk In Joiner", "plan": "Monthly"})
    assert r.status_code == 200, r.text
    member = r.json()["member"]
    # The UI contract: the created member comes back with the fields the
    # Add Member form shows — above all the auto-given PHG number.
    assert member["code"] == "PHG-049"  # next sequential, assigned by the system
    assert member["name"] == "Walk In Joiner"
    assert member["phone"] == ""
    assert member["plan"] == "Monthly"
    assert member["checked_in_today"] is False
    assert "PHG-049" in r.json()["message"]

    before = auth.get("/api/stats").json()
    assert before["checked_in_today"] == 5

    token = _token(auth)
    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-049"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["member_name"] == "Walk In Joiner"
    assert body["member_code"] == "PHG-049"
    assert body["date"] == date.today().isoformat()
    assert body["time"]

    today = auth.get("/api/checkins/today").json()["checkins"]
    entry = next(c for c in today if c["code"] == "PHG-049")
    assert entry["name"] == "Walk In Joiner"
    assert entry["source"] == "qr"
    assert entry["time"] == body["time"]

    after = auth.get("/api/stats").json()
    assert after["checked_in_today"] == before["checked_in_today"] + 1
    assert after["total_members"] == len(members) + 1  # 48 seed + the new joiner
