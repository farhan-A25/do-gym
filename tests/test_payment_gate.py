"""Part 2 (owner order, 2026-10-23): PAY FIRST, THEN THE WEBSITE WORKS.

The pay-first gate, proven end-to-end against the real app:
  * an authenticated owner whose subscription is not ACTIVE — never
    paid, a legacy "payment_pending" state from the removed Pay Later
    button, or a paid period that has ended — is refused by every app
    API with 402 {"code": "payment_required"} (a direct call, not just
    frontend hiding), and keeps only the entry-flow + payment endpoints;
  * a payment recorded by FARHAN (in-person collection, admin
    activation — owners cannot activate themselves since 2026-10-23)
    lifts the gate immediately;
  * an expired plan re-gates, and renewing re-opens;
  * the demo account's seeded PAID subscription keeps it ACTIVE;
  * the public member check-in is never gated.

Subscription state is global to the install (settings + payment rows),
so tests flip it through the database and an autouse fixture always
hands the next test an ACTIVE subscription back.
"""
from datetime import datetime, timedelta

import pytest

import main
import models
from conftest import admin_activate
from database import SessionLocal

DEMO = {"email": "owner@powerhousegym.in", "password": "Gym@2026"}

GATED_GETS = [
    "/api/stats", "/api/members", "/api/red-list", "/api/renewals",
    "/api/checkins/today", "/api/gym/checkin-link",
    "/api/mode", "/api/search?q=rohit",
]


@pytest.fixture(autouse=True)
def _leave_subscription_active(auth):
    """Every test starts from the seeded demo data (the `auth` fixture
    resets it); after the test, activate once (as Farhan would) so the
    subscription the next test inherits is ACTIVE again."""
    yield
    admin_activate(auth)


def _set_settings(**settings):
    with SessionLocal() as db:
        for key, value in settings.items():
            main.set_setting(db, key, value)
        db.commit()


def _make_unpaid():
    """No payment recorded at all — a fresh owner who never paid."""
    with SessionLocal() as db:
        db.query(models.SubscriptionPayment).delete()
        db.commit()
    _set_settings(plan_status="", payment_status="")


def _make_legacy_pending():
    """The old Pay Later state: a 'pending' payment row + pending flags."""
    with SessionLocal() as db:
        user = (db.query(models.User)
                .filter(models.User.email == DEMO["email"]).first())
        db.add(models.SubscriptionPayment(
            user_id=user.id, plan_id="starter", plan_name="Standard",
            period="monthly", amount=1599, method="upi_qr",
            status="pending", created_at=datetime.now(),
        ))
        db.commit()
    _set_settings(plan_id="starter", plan_period="monthly",
                  plan_status="payment_pending",
                  payment_status="payment_pending")


def _backdate_latest_payment(days):
    with SessionLocal() as db:
        row = (db.query(models.SubscriptionPayment)
               .order_by(models.SubscriptionPayment.id.desc()).first())
        assert row is not None and row.status == "paid"
        row.created_at = datetime.now() - timedelta(days=days)
        db.commit()


# ------------------------------------------------------------- the gate ----
def test_unpaid_owner_blocked_from_all_app_apis(auth):
    _make_unpaid()
    for path in GATED_GETS:
        r = auth.get(path)
        assert r.status_code == 402, f"{path} returned {r.status_code}"
        assert r.json()["code"] == "payment_required"
    # Writes are gated too — no sneaking data in while unpaid.
    r = auth.post("/api/checkin", json={"member_id": 1})
    assert r.status_code == 402
    r = auth.post("/api/members", json={"name": "Gate Test", "plan": "Monthly"})
    assert r.status_code == 402
    r = auth.put("/api/mode", json={"mode": "manual"})
    assert r.status_code == 402


def test_unpaid_owner_keeps_only_the_payment_step(auth, client):
    _make_unpaid()
    # Session + entry flow stay reachable.
    assert auth.get("/api/me").status_code == 200
    assert auth.get("/api/details").status_code == 200
    # Plans + payment step stay reachable, and report the gated state.
    plan = auth.get("/api/plan")
    assert plan.status_code == 200
    assert plan.json()["subscription_active"] is False
    assert plan.json()["subscription_status"] == "unpaid"
    assert auth.get("/api/plans").status_code == 200
    assert auth.get("/payment-qr.png").status_code == 200
    # Page loads still serve the SPA shell + legal pages — the frontend
    # routes a gated owner straight to the payment step from /api/plan.
    assert client.get("/").status_code == 200
    assert client.get("/privacy").status_code == 200
    assert client.get("/terms").status_code == 200


def test_choosing_a_plan_alone_does_not_unlock(auth):
    """plan/switch records the choice; only a payment lifts the gate."""
    _make_unpaid()
    r = auth.post("/api/plan/switch", json={"plan_id": "starter", "period": "yearly"})
    assert r.status_code == 200
    assert auth.get("/api/me").json()["plan_chosen"] is True
    r = auth.get("/api/stats")
    assert r.status_code == 402
    assert r.json()["code"] == "payment_required"


def test_legacy_payment_pending_is_treated_as_unpaid(auth):
    _make_legacy_pending()
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is False
    assert plan["subscription_status"] == "payment_pending"
    r = auth.get("/api/stats")
    assert r.status_code == 402
    assert r.json()["subscription_status"] == "payment_pending"
    # …and Farhan recording the collection clears it for good.
    admin_activate(auth, plan_id="starter", period="monthly")
    assert auth.get("/api/stats").status_code == 200


# ------------------------------------------------------ paying opens it ----
def test_admin_activation_lifts_the_gate_immediately(auth):
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402

    # The owner cannot self-activate — the old route is deleted. (While
    # gated, the gate itself answers 402 before routing; the property
    # that matters is: no unlock happens.)
    r = auth.post("/api/subscription/confirm-payment",
                  json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code in (402, 404)
    assert auth.get("/api/stats").status_code == 402

    # Farhan collects in person and activates from Control.
    result = admin_activate(auth, plan_id="starter", period="monthly")
    assert result["recorded_payment"] is True
    state = auth.get("/api/plan").json()
    assert state["payment_status"] == "active"
    assert state["subscription_active"] is True
    # Standard monthly at the owner's 2026-10-23 price, computed server-side.
    assert state["last_payment"]["amount"] == 1599
    assert state["last_payment"]["status"] == "paid"
    assert state["last_payment"]["method"] == "collected_in_person"

    # The very same calls that were refused now succeed.
    assert auth.get("/api/stats").status_code == 200
    assert auth.get("/api/members").status_code == 200
    assert auth.get("/api/red-list").status_code == 200
    assert auth.get("/api/mode").status_code == 200
    # …and with the gate open, the deleted self-confirm route is a
    # plain 404 — there is no way for an owner to record a payment.
    r = auth.post("/api/subscription/confirm-payment",
                  json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code == 404


def test_standard_yearly_payment_records_new_price(auth):
    admin_activate(auth, plan_id="starter", period="yearly")
    state = auth.get("/api/plan").json()
    assert state["last_payment"]["amount"] == 15990
    assert state["last_payment"]["method"] == "collected_in_person"


# ---------------------------------------------------------------- expiry ----
def test_expired_monthly_plan_regates_and_renewal_reopens(auth):
    # Seeded demo subscription (Premium monthly, paid today) works…
    assert auth.get("/api/stats").status_code == 200
    # …until its 30-day period ends.
    _backdate_latest_payment(40)
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is False
    assert plan["subscription_status"] == "expired"
    r = auth.get("/api/stats")
    assert r.status_code == 402
    assert r.json()["subscription_status"] == "expired"
    # Renewing (Farhan collects + activates again) re-opens immediately.
    admin_activate(auth, plan_id="pro", period="monthly")
    assert auth.get("/api/stats").status_code == 200


def test_yearly_plan_stays_active_past_the_monthly_window(auth):
    admin_activate(auth, plan_id="pro", period="yearly")
    _backdate_latest_payment(40)  # past 30 days, well inside 365
    assert auth.get("/api/stats").status_code == 200
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is True


# ---------------------------------------------------- demo + public scan ----
def test_demo_account_is_active_from_its_seeded_payment(auth):
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is True
    assert plan["subscription_status"] == "active"
    # Seeded as a real paid record (Premium monthly, billing.py price).
    lp = plan["last_payment"]
    assert lp["status"] == "paid"
    assert lp["amount"] == 1899
    assert lp["plan_name"] == "Premium"
    # Nothing in the app is gated for the demo owner.
    for path in GATED_GETS:
        assert auth.get(path).status_code == 200, path


def test_public_member_scan_works_for_a_gated_gym(auth, client):
    token = auth.get("/api/gym/checkin-link").json()["token"]
    _make_unpaid()
    # The owner is locked out…
    assert auth.get("/api/stats").status_code == 402
    # …but members keep scanning in against the same gym.
    r = client.get(f"/checkin/{token}")
    assert r.status_code == 200
    r = client.post("/api/checkin/scan", json={"token": token, "code": "PHG-001"})
    assert r.status_code == 200, r.text
    assert r.json()["member_name"] == "Rohit A."
    assert r.json()["already_checked_in"] is False
