"""Tenant isolation (owner correction, 2026-10-24).

The member register used to be one install-wide list: every signed-in
owner saw the demo gym's 48 members. Farhan's brother signed in on his
own phone and saw that fake data instead of his own empty gym.

These tests pin the corrected behaviour:
  * a brand-new gym starts EMPTY everywhere (members, stats, Red List,
    renewals, search, plan usage) while the demo gym keeps its 48;
  * PHG codes, check-in tokens and the AUTO/MANUAL switch are per gym;
  * no owner can check in, flag, clear, renew or unflag another gym's
    member — the other gym's data answers 404, as if it did not exist;
  * the public wall-QR scan resolves the token to ITS gym and only
    ever matches codes inside that gym;
  * resetting the demo data restores the demo gym only — a real gym's
    members, token and payments survive it untouched.
"""
from fastapi.testclient import TestClient

import main
from conftest import admin_activate

DEMO_EMAIL = "owner@powerhousegym.in"


def _make_owner(email: str, gym_name: str):
    """A fully onboarded, Farhan-activated gym owner on its own client."""
    c = TestClient(main.app)
    r = c.post("/api/signup", json={
        "owner_name": "Tenant Owner", "gym_name": gym_name,
        "email": email, "mobile": "9812345678", "password": "secret6",
    })
    assert r.status_code == 200, r.text
    r = c.post("/api/auth/otp/verify", json={"code": r.json()["dev_otp"]})
    assert r.status_code == 200, r.text
    r = c.put("/api/details", json={
        "gym_name": gym_name, "gym_address": "Road 9", "gym_city": "Warangal",
        "gym_phone": "9812345678", "opening_year": 2024,
        "owner_name": "Tenant Owner", "mobile": "9812345678", "whatsapp": "",
    })
    assert r.status_code == 200, r.text
    r = c.post("/api/plan/switch", json={"plan_id": "pro", "period": "monthly"})
    assert r.status_code == 200, r.text
    admin_activate(c, email=email, plan_id="pro", period="monthly")
    return c


def _add(client, name, plan="Monthly", **extra):
    r = client.post("/api/members", json={"name": name, "plan": plan, **extra})
    assert r.status_code == 200, r.text
    return r.json()["member"]


# ------------------------------------------------------------ empty start ----
def test_new_gym_starts_empty_and_demo_keeps_its_register(auth):
    b = _make_owner("tenant-empty@test.in", "Empty Start Gym")

    assert b.get("/api/members").json()["members"] == []
    assert b.get("/api/red-list").json()["members"] == []
    assert b.get("/api/search", params={"q": "Rohit"}).json()["members"] == []

    s = b.get("/api/stats").json()
    assert s["total_members"] == 0
    assert s["red_list_count"] == 0
    assert s["checked_in_today"] == 0
    assert s["renewals_due"] == 0
    assert s["expired_count"] == 0
    assert s["renewed_this_month"] == 0

    ren = b.get("/api/renewals").json()
    assert ren["due"] == [] and ren["expired"] == [] and ren["history"] == []
    assert b.get("/api/plan").json()["usage"]["members_used"] == 0

    # The demo gym is untouched by the new gym existing at all.
    assert auth.get("/api/stats").json()["total_members"] == 48
    assert auth.get("/api/plan").json()["usage"]["members_used"] == 48


def test_each_gym_has_its_own_phg_sequence_and_mode(auth):
    b = _make_owner("tenant-codes@test.in", "Codes Gym")

    mine = _add(b, "Beta First")
    assert mine["code"] == "PHG-001"  # B's sequence starts at 001…
    demo_new = _add(auth, "Demo Extra")
    assert demo_new["code"] == "PHG-049"  # …while the demo's runs on to 049

    assert b.get("/api/stats").json()["total_members"] == 1
    assert auth.get("/api/stats").json()["total_members"] == 49
    assert b.get("/api/plan").json()["usage"]["members_used"] == 1
    assert auth.get("/api/plan").json()["usage"]["members_used"] == 49

    # The AUTO/MANUAL switch is per gym too.
    auth.put("/api/mode", json={"mode": "auto"})
    b.put("/api/mode", json={"mode": "manual"})
    assert b.get("/api/mode").json()["mode"] == "manual"
    assert auth.get("/api/mode").json()["mode"] == "auto"
    assert auth.get("/api/stats").json()["mode"] == "auto"


# ------------------------------------------------------- cross-gym denial ----
def test_owner_b_cannot_touch_owner_a_members(auth):
    b = _make_owner("tenant-idor@test.in", "Idor Gym")
    _add(b, "Beta Member")

    demo_members = auth.get("/api/members").json()["members"]
    rohit = next(m for m in demo_members if m["name"] == "Rohit A.")

    # Check-in by id and by a code that exists only in the demo gym.
    assert b.post("/api/checkin", json={"member_id": rohit["id"]}).status_code == 404
    assert b.post("/api/checkin", json={"code": "PHG-048"}).status_code == 404
    # Red List flag / clear / unflag.
    assert b.post("/api/red-list/flag", json={"member_id": rohit["id"]}).status_code == 404
    assert b.post("/api/red-list/clear", json={"member_id": rohit["id"]}).status_code == 404
    assert b.delete(f"/api/red-list/flag/{rohit['id']}").status_code == 404
    # Renewal of a demo member.
    assert b.post(f"/api/renewals/{rohit['id']}/renew", json={}).status_code == 404

    # B's own member, found by B's own PHG-001, works normally — and the
    # demo gym's Rohit (also PHG-001, in HIS gym) is a different person.
    r = b.post("/api/checkin", json={"code": "PHG-001"})
    assert r.status_code == 200, r.text
    assert r.json()["member"]["name"] == "Beta Member"
    demo_today = [c["name"] for c in auth.get("/api/checkins/today").json()["checkins"]]
    assert "Beta Member" not in demo_today


def test_renewals_and_history_are_per_gym(auth):
    b = _make_owner("tenant-renew@test.in", "Renewal Gym")
    expired = _add(b, "Beta Expired", expiry_date="2020-01-01")
    assert b.get("/api/renewals").json()["stats"]["expired_count"] == 1
    assert auth.get("/api/renewals").json()["stats"]["expired_count"] == 3

    r = b.post(f"/api/renewals/{expired['id']}/renew", json={})
    assert r.status_code == 200, r.text
    b_ren = b.get("/api/renewals").json()
    assert [h["member_name"] for h in b_ren["history"]] == ["Beta Expired"]
    assert b_ren["stats"]["renewed_this_month"] == 1000

    demo_ren = auth.get("/api/renewals").json()
    assert all(h["member_name"] != "Beta Expired" for h in demo_ren["history"])
    assert demo_ren["stats"]["renewed_this_month"] == 2000


# ------------------------------------------------------------- wall QR ----
def test_qr_tokens_codes_and_pages_are_per_gym(auth, client):
    b = _make_owner("tenant-qr@test.in", "Beta Fitness Club")
    beta = _add(b, "Beta Scanner")

    demo_link = auth.get("/api/gym/checkin-link").json()
    beta_link = b.get("/api/gym/checkin-link").json()
    assert beta_link["token"] != demo_link["token"]
    assert beta_link["gym_name"] == "Beta Fitness Club"

    page = client.get(f"/checkin/{beta_link['token']}")
    assert page.status_code == 200
    assert "Beta Fitness Club" in page.text
    assert "PowerHouse Gym" not in page.text

    # Same code, different gym: each token checks in its OWN PHG-001.
    r = client.post("/api/checkin/scan",
                    json={"token": beta_link["token"], "code": beta["code"]})
    assert r.status_code == 200, r.text
    assert r.json()["member_name"] == "Beta Scanner"
    assert r.json()["gym_name"] == "Beta Fitness Club"

    r = client.post("/api/checkin/scan",
                    json={"token": demo_link["token"], "code": "PHG-001"})
    assert r.status_code == 200, r.text
    assert r.json()["member_name"] == "Rohit A."

    # A code that exists only in the demo gym is invisible through B's poster.
    r = client.post("/api/checkin/scan",
                    json={"token": beta_link["token"], "code": "PHG-048"})
    assert r.status_code == 404

    # Resetting B's poster retires B's old link only; the demo's lives on.
    new_token = b.post("/api/gym/checkin-link/reset").json()["token"]
    assert client.get(f"/checkin/{beta_link['token']}").status_code == 404
    assert client.get(f"/checkin/{new_token}").status_code == 200
    assert client.get(f"/checkin/{demo_link['token']}").status_code == 200


def test_demo_reset_restores_demo_gym_only(auth):
    b = _make_owner("tenant-reset@test.in", "Reset Safe Gym")
    _add(b, "Survivor Member")
    beta_token = b.get("/api/gym/checkin-link").json()["token"]

    r = auth.post("/api/reset")
    assert r.status_code == 200, r.text

    # Demo gym restored…
    assert auth.get("/api/stats").json()["total_members"] == 48
    # …and the real gym's data, poster and access all survived it.
    names = [m["name"] for m in b.get("/api/members").json()["members"]]
    assert names == ["Survivor Member"]
    assert b.get("/api/gym/checkin-link").json()["token"] == beta_token
    assert b.get("/api/stats").status_code == 200
