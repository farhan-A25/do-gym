"""Automatic unlock (owner order, 2026-10-23): NO confirmation buttons.

"I Have Paid" is gone from the UI — the owner pays, the QR/panel turns
green ("Payment Done ✓") and the website unlocks BY ITSELF. Proven
here, fully offline:

  * the served owner page contains neither "I Have Paid" nor
    "Pay Later", and the payment panel is QR-ONLY: no checkout/pay
    button, no Razorpay Checkout script, no order/confirm calls —
    just Farhan's QR, the waiting line and the green "Payment Done ✓";
  * with Razorpay unconfigured, the dormant order endpoint answers
    404 and the webhook answers 404;
  * with Razorpay configured (order creation stubbed — no live calls),
    the order endpoint returns the server-computed amount and stores
    the order with the owner's id;
  * a webhook with a VALID self-computed HMAC signature records the
    payment (method "razorpay") and lifts the pay-first gate — 402
    becomes 200 with no manual confirm anywhere;
  * a BAD signature records nothing and the gate stays shut;
  * the same webhook delivered twice records exactly one payment;
  * order.paid works as well as payment.captured.

Subscription state is global to the install, so an autouse fixture
hands every test the seeded demo data and leaves the subscription
ACTIVE again afterwards (same approach as test_payment_gate.py).
"""
import hashlib
import hmac
import json
import uuid

import pytest

import main
import models
import payments_razorpay
from conftest import admin_activate
from database import SessionLocal

DEMO = {"email": "owner@powerhousegym.in", "password": "Gym@2026"}
KEY_ID = "rzp_test_keyid123"
KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _leave_subscription_active(auth):
    yield
    admin_activate(auth)


@pytest.fixture()
def gateway_on(monkeypatch):
    """Razorpay configured, with order creation stubbed (offline)."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    created = []

    def fake_create_order(amount_paise, receipt, notes):
        created.append({"amount_paise": amount_paise, "receipt": receipt,
                        "notes": notes})
        # Unique per call across the whole run — gateway_orders.order_id
        # is unique and the table is not wiped by the demo reset.
        return {"id": f"order_TEST{len(created)}_{uuid.uuid4().hex[:8]}",
                "amount": amount_paise, "currency": "INR", "status": "created"}

    monkeypatch.setattr(payments_razorpay, "create_order", fake_create_order)
    return created


@pytest.fixture()
def gateway_off(monkeypatch):
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)


def _make_unpaid():
    with SessionLocal() as db:
        db.query(models.SubscriptionPayment).delete()
        db.commit()
    with SessionLocal() as db:
        main.set_setting(db, "plan_status", "")
        main.set_setting(db, "payment_status", "")
        db.commit()


def _demo_user_id():
    with SessionLocal() as db:
        user = db.query(models.User).filter(models.User.email == DEMO["email"]).first()
        return user.id


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json",
                 "X-Razorpay-Signature": _sign(body, secret)},
    )


def _captured_payload(order_id, payment_id, amount_paise, user_id):
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id,
            "amount": amount_paise, "currency": "INR", "status": "captured",
            "notes": {"user_id": str(user_id)},
        }}},
    }


def _create_order(auth, plan_id="starter", period="monthly"):
    r = auth.post("/api/subscription/order", json={"plan_id": plan_id, "period": period})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- the UI ----
def test_served_panel_is_qr_plus_face_pay_button(client):
    """Panel rule (owner's orders, 2026-10-23): his QR + the plan/amount
    + exactly ONE action — the Pay button that starts the face scan.
    No confirmation buttons of any era, no checkout button, and NO
    passive unlock: the panel must not poll its way open."""
    page = client.get("/")
    assert page.status_code == 200
    # No confirmation buttons of any era.
    assert "I Have Paid" not in page.text
    assert "Pay Later" not in page.text
    # No checkout / gateway wiring in the panel.
    assert "Pay ₹" not in page.text
    assert "pay-now" not in page.text
    assert "checkout.razorpay.com" not in page.text
    assert "/api/subscription/order" not in page.text
    assert "/api/subscription/confirm-payment" not in page.text
    # The panel itself: his QR, the ONE Pay (face-scan) button, the
    # green state, and the in-person payment line.
    assert "/payment-qr.png" in page.text
    assert 'id="pay-face-btn"' in page.text
    assert "Payment Done" in page.text
    assert "pay when our team visits your gym" in page.text
    # No passive unlock machinery anywhere in the served owner UI.
    assert "Waiting for payment" not in page.text
    assert "checkPaymentDone" not in page.text
    assert "startPaymentWatch" not in page.text
    # Farhan's control page is never linked from the owner UI.
    assert '"/control"' not in page.text
    assert 'href="/control"' not in page.text


# ------------------------------------------------- gateway not configured ----
def test_order_endpoint_disabled_without_keys(auth, gateway_off):
    r = auth.post("/api/subscription/order",
                  json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code == 404  # dormant gateway plumbing stays off without keys


def test_webhook_disabled_without_secret(client, gateway_off):
    r = _post_webhook(client, {"event": "payment.captured", "payload": {}})
    assert r.status_code == 404


# ---------------------------------------------------- order creation ----
def test_order_creation_uses_server_amount_and_stores_owner(auth, gateway_on):
    data = _create_order(auth, "starter", "monthly")
    assert data["order_id"].startswith("order_TEST")
    assert data["key_id"] == KEY_ID
    # Standard monthly at the owner's price — computed server-side.
    assert data["amount"] == 1599
    assert data["amount_paise"] == 159900
    # Razorpay was asked for exactly that, in paise, with the owner in notes.
    (call,) = gateway_on
    assert call["amount_paise"] == 159900
    assert call["notes"]["user_id"] == str(_demo_user_id())
    assert call["notes"]["plan_id"] == "starter"
    # And the order is on file for the webhook to match against.
    with SessionLocal() as db:
        row = db.query(models.GatewayOrder).filter(
            models.GatewayOrder.order_id == data["order_id"]).first()
        assert row is not None
        assert row.user_id == _demo_user_id()
        assert row.amount == 1599
        assert row.status == "created"


def test_order_requires_login(gateway_on):
    # A brand-new client: this module's autouse fixture logs the shared
    # one in, which would defeat the point of this test.
    from fastapi.testclient import TestClient
    fresh = TestClient(main.app)
    r = fresh.post("/api/subscription/order",
                   json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code == 401


# ------------------------------------------------- webhook → unlock ----
def test_valid_webhook_records_payment_and_lifts_gate(auth, client, gateway_on):
    order = _create_order(auth, "starter", "monthly")
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402  # gate shut

    payload = _captured_payload(order["order_id"], "pay_TEST1", 159900,
                                _demo_user_id())
    r = _post_webhook(client, payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recorded"

    # The gate lifted BY ITSELF — no confirm-payment call anywhere.
    assert auth.get("/api/stats").status_code == 200
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is True
    lp = plan["last_payment"]
    assert lp["status"] == "paid"
    assert lp["method"] == "razorpay"
    assert lp["amount"] == 1599
    with SessionLocal() as db:
        row = db.query(models.SubscriptionPayment).filter(
            models.SubscriptionPayment.gateway_payment_id == "pay_TEST1").first()
        assert row is not None and row.gateway_order_id == order["order_id"]
        grow = db.query(models.GatewayOrder).filter(
            models.GatewayOrder.order_id == order["order_id"]).first()
        assert grow.status == "paid" and grow.payment_id == "pay_TEST1"


def test_order_paid_event_also_records(auth, client, gateway_on):
    order = _create_order(auth, "pro", "yearly")
    _make_unpaid()
    payload = {
        "event": "order.paid",
        "payload": {
            "order": {"entity": {"id": order["order_id"],
                                 "notes": {"user_id": str(_demo_user_id())}}},
            "payment": {"entity": {"id": "pay_TEST9",
                                   "order_id": order["order_id"],
                                   "amount": 1899000}},
        },
    }
    r = _post_webhook(client, payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recorded"
    assert auth.get("/api/stats").status_code == 200
    plan = auth.get("/api/plan").json()
    assert plan["last_payment"]["amount"] == 18990  # Premium yearly
    assert plan["last_payment"]["method"] == "razorpay"


def test_bad_signature_records_nothing(auth, client, gateway_on):
    order = _create_order(auth, "starter", "monthly")
    _make_unpaid()
    payload = _captured_payload(order["order_id"], "pay_EVIL", 159900,
                                _demo_user_id())
    r = _post_webhook(client, payload, secret="the-wrong-secret")
    assert r.status_code == 400
    assert auth.get("/api/stats").status_code == 402  # still locked
    with SessionLocal() as db:
        assert db.query(models.SubscriptionPayment).count() == 0


def test_duplicate_webhook_records_exactly_once(auth, client, gateway_on):
    order = _create_order(auth, "starter", "monthly")
    _make_unpaid()
    payload = _captured_payload(order["order_id"], "pay_DUP1", 159900,
                                _demo_user_id())
    r1 = _post_webhook(client, payload)
    assert r1.status_code == 200 and r1.json()["status"] == "recorded"
    # Razorpay retries / duplicate deliveries must not double-record.
    r2 = _post_webhook(client, payload)
    assert r2.status_code == 200 and r2.json()["status"] == "already_recorded"
    with SessionLocal() as db:
        rows = db.query(models.SubscriptionPayment).filter(
            models.SubscriptionPayment.gateway_payment_id == "pay_DUP1").all()
        assert len(rows) == 1


def test_webhook_for_unknown_order_is_ignored(auth, client, gateway_on):
    _make_unpaid()
    payload = _captured_payload("order_NOTOURS", "pay_GHOST", 159900,
                                _demo_user_id())
    r = _post_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert auth.get("/api/stats").status_code == 402
    with SessionLocal() as db:
        assert db.query(models.SubscriptionPayment).count() == 0


def test_webhook_amount_mismatch_is_ignored(auth, client, gateway_on):
    order = _create_order(auth, "starter", "monthly")  # ₹1,599 = 159900 paise
    _make_unpaid()
    payload = _captured_payload(order["order_id"], "pay_SHORT", 100,
                                _demo_user_id())
    r = _post_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert auth.get("/api/stats").status_code == 402
