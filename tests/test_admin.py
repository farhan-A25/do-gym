"""Platform admin — Farhan's Control (owner model, 2026-10-23).

Farhan visits each gym, collects the subscription money in person,
then activates that gym from Control. Proven end-to-end here:

  * with ADMIN_SECRET unset, /control and the admin API do not exist
    (404) — and with it set, the API demands the X-Admin-Secret header;
  * the /control login refuses wrong passwords and rate-limits;
  * the page + API list every gym (owner, gym, email, provider badge,
    mobile, plan + period, status, expiry, joined) with summary
    totals that match the database;
  * Activate (page form AND API) records the collection
    (method "collected_in_person") and lifts the gym's gate at once —
    owner APIs 402 → 200 — and never double-records a live window;
  * Switch Off locks an active gym instantly and deletes NOTHING;
    Activate restores it with members + check-ins intact;
  * Google-created accounts show provider "google" in the admin list
    (and the "Google ✓ verified" tag on the page); email signups
    show "email".

Subscription state is global to the install, so every test arranges
its own; an autouse fixture clears switch-off flags afterwards.
"""
import re
import urllib.parse

import pytest
from sqlalchemy import func

import main
import models
from database import SessionLocal

DEMO_EMAIL = "owner@powerhousegym.in"
CONTROL_SECRET = "test-control-secret"
HEADERS = {"X-Admin-Secret": CONTROL_SECRET}


@pytest.fixture()
def admin_on(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", CONTROL_SECRET)


@pytest.fixture()
def admin_off(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _leave_active_and_unrevoked():
    """Restore the suite-wide invariant after every test here: no
    switch-off flags left set, and the demo subscription ACTIVE (some
    tests here deliberately leave it unpaid, which would otherwise
    gate even /api/reset for the next module's fixtures)."""
    yield
    with SessionLocal() as db:
        db.query(models.Setting).filter(
            models.Setting.key.like("revoked_user_%")
        ).delete(synchronize_session=False)
        db.commit()
    from fastapi.testclient import TestClient

    from conftest import admin_activate as _activate
    try:
        _activate(TestClient(main.app))
    except AssertionError:
        pass  # a later fixture reset will surface any real problem


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
        return db.query(models.User).filter(
            models.User.email == DEMO_EMAIL).first().id


def _db_counts():
    with SessionLocal() as db:
        return {
            "members": db.query(models.Member).count(),
            "checkins": db.query(models.CheckIn).count(),
            "collected": int(
                db.query(func.sum(models.SubscriptionPayment.amount))
                .filter(models.SubscriptionPayment.status == "paid")
                .scalar() or 0
            ),
        }


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field found in page"
    return m.group(1)


def _control_login(client, password=CONTROL_SECRET):
    """Log into /control through the real form; returns the dashboard."""
    r = client.get("/control")
    assert r.status_code == 200
    assert "Open Control" in r.text  # the login form, not the dashboard
    r = client.post("/control/login", data={
        "password": password, "csrf": _csrf_from(r.text),
    })
    assert r.status_code == 200, r.text[:300]
    assert "Total gyms" in r.text  # the dashboard
    return r


def _admin_list(client):
    r = client.get("/api/admin/gyms", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _account(summary, email):
    return next(a for a in summary["accounts"] if a["email"] == email)


# ------------------------------------------------------------ existence ----
def test_admin_routes_do_not_exist_without_secret(client, admin_off):
    assert client.get("/control").status_code == 404
    assert client.post("/control/login", data={"password": "x"}).status_code == 404
    assert client.get("/api/admin/gyms", headers=HEADERS).status_code == 404
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"email": DEMO_EMAIL, "plan_id": "pro", "period": "monthly"})
    assert r.status_code == 404
    r = client.post("/api/admin/switch-off", headers=HEADERS,
                    json={"email": DEMO_EMAIL})
    assert r.status_code == 404


def test_admin_api_requires_the_secret(client, admin_on):
    assert client.get("/api/admin/gyms").status_code == 403
    r = client.get("/api/admin/gyms", headers={"X-Admin-Secret": "wrong"})
    assert r.status_code == 403
    summary = _admin_list(client)
    assert summary["total_gyms"] >= 1
    account = _account(summary, DEMO_EMAIL)
    for field in ("owner_name", "gym_name", "email", "mobile",
                  "auth_provider", "joined", "plan_id", "period",
                  "status", "expires_on"):
        assert field in account, field
    assert account["auth_provider"] == "email"  # seeded demo account


# --------------------------------------------------------- control login ----
def test_control_login_wrong_password_refused(client, admin_on):
    r = client.get("/control")
    assert r.status_code == 200
    assert "Admin password" in r.text
    r = client.post("/control/login", data={
        "password": "not-the-secret", "csrf": _csrf_from(r.text),
    })
    assert r.status_code == 401
    assert "Wrong password" in r.text
    # Still locked out afterwards.
    r = client.get("/control")
    assert "Open Control" in r.text
    assert "Total gyms" not in r.text


def test_control_login_is_rate_limited(client, admin_on):
    r = client.get("/control")
    csrf = _csrf_from(r.text)
    for _ in range(5):
        r = client.post("/control/login", data={
            "password": "wrong-again", "csrf": csrf,
        })
        assert r.status_code == 401
    # The 6th attempt is refused even with the CORRECT password.
    r = client.post("/control/login", data={
        "password": CONTROL_SECRET, "csrf": csrf,
    })
    assert r.status_code == 429


def test_admin_cookie_scoping_and_logout(client, admin_on):
    r = client.get("/control")
    r = client.post("/control/login", data={
        "password": CONTROL_SECRET, "csrf": _csrf_from(r.text),
    }, follow_redirects=False)
    assert r.status_code == 303
    cookies = [c for c in r.headers.get_list("set-cookie") if c.startswith("phg_admin=")]
    assert len(cookies) == 3  # one per scope: /control, /api/admin, /api/control
    assert any("Path=/control" in c for c in cookies)
    assert any("Path=/api/admin" in c for c in cookies)
    assert any("Path=/api/control" in c for c in cookies)
    assert all("HttpOnly" in c for c in cookies)
    # The cookie opens the dashboard…
    r = client.get("/control")
    assert "Total gyms" in r.text
    # …and Logout closes it again.
    r = client.post("/control/logout", data={"csrf": _csrf_from(r.text)})
    assert r.status_code == 200
    r = client.get("/control")
    assert "Open Control" in r.text
    assert "Total gyms" not in r.text


def test_control_posts_require_csrf(auth, client, admin_on):
    _control_login(client)
    uid = _demo_user_id()
    r = client.post("/control/activate", data={"user_id": uid})
    assert r.status_code == 403
    r = client.post("/control/activate", data={"user_id": uid, "csrf": "bogus"})
    assert r.status_code == 403
    # Cookie-authenticated API calls need the CSRF value too…
    r = client.post("/api/admin/activate", json={"email": DEMO_EMAIL})
    assert r.status_code == 403
    # …while the secret header is its own credential (no CSRF needed).
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"email": DEMO_EMAIL})
    assert r.status_code == 200


# ------------------------------------------------------------- the list ----
def test_control_page_lists_gyms_statuses_and_totals(auth, client, admin_on):
    dashboard = _control_login(client)
    page = dashboard.text
    # The seeded demo gym, active, with everything Farhan asked to see.
    assert "PowerHouse Gym" in page
    assert DEMO_EMAIL in page
    assert "Premium" in page
    assert "Active ✓" in page
    assert "Google ✓ verified" not in page  # demo account came via email
    assert ">Email<" in page
    counts = _db_counts()
    assert f"₹{main._inr(counts['collected'])}" in page
    # A second gym signs up — it appears too, as Not Paid.
    from fastapi.testclient import TestClient
    other = TestClient(main.app)
    r = other.post("/api/signup", json={
        "owner_name": "Second Owner", "gym_name": "Second Gym",
        "email": "second@example.in", "mobile": "9812345670",
        "password": "secret123",
    })
    assert r.status_code == 200, r.text
    page = client.get("/control").text
    assert "Second Gym" in page
    assert "second@example.in" in page
    # Wipe the payments: the demo gym now reads Not Paid, with Activate.
    _make_unpaid()
    page = client.get("/control").text
    assert "Not Paid" in page
    assert 'action="/control/activate"' in page
    # Leave the gym active again for the next test.
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"email": DEMO_EMAIL, "plan_id": "pro",
                          "period": "monthly"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------- activation ----
def test_admin_api_activation_lifts_gate_end_to_end(auth, client, admin_on):
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402  # gate shut

    r = client.post("/api/admin/activate", headers=HEADERS, json={
        "email": DEMO_EMAIL, "plan_id": "starter", "period": "monthly",
    })
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["recorded_payment"] is True
    assert result["account"]["status"] == "active"
    total_after_first = result["summary"]["total_collected"]

    # The owner's app is open, showing the in-person collection.
    assert auth.get("/api/stats").status_code == 200
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is True
    assert plan["last_payment"]["method"] == "collected_in_person"
    assert plan["last_payment"]["amount"] == 1599  # Standard monthly, server-side

    # Activating again inside the live window records NOTHING twice.
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"user_id": _demo_user_id()})
    assert r.status_code == 200
    assert r.json()["recorded_payment"] is False
    assert r.json()["summary"]["total_collected"] == total_after_first

    # Unknown accounts are a clean 404, never a 500.
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"email": "ghost@example.in", "plan_id": "pro",
                          "period": "monthly"})
    assert r.status_code == 404

    # …and it all survives a fresh client (a real recorded payment).
    from fastapi.testclient import TestClient
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", json={"email": DEMO_EMAIL, "password": "Gym@2026"})
    assert r.status_code == 200
    state = fresh.get("/api/plan").json()
    assert state["payment_status"] == "active"
    assert state["last_payment"]["method"] == "collected_in_person"


def _page_face_proof(client, csrf):
    """Stub a passed face verification for a Control page Activate,
    exactly the seam the page uses: server challenge in, proof fields
    (challenge + chosen liveness + under-threshold distance) out."""
    ch = client.post("/api/admin/face-challenge", json={"csrf": csrf})
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    return {
        "face_challenge_id": ch["challenge_id"],
        "face_distance": "0.30",
        "face_liveness": ch["action"],
    }


def test_control_page_activate_button_lifts_gate(auth, client, admin_on):
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402
    # Farhan's face is enrolled; the page's Activate passes a face
    # verification (stubbed here at the server seam, as in the
    # face-unlock tests: challenge + chosen liveness + distance).
    r = client.post("/api/admin/face-template", headers=HEADERS,
                    json={"descriptors": [[0.01] * 128]})
    assert r.status_code == 200, r.text
    try:
        dashboard = _control_login(client)
        assert 'action="/control/activate"' in dashboard.text
        csrf = _csrf_from(dashboard.text)
        r = client.post("/control/activate", data={
            "user_id": _demo_user_id(), "csrf": csrf,
            **_page_face_proof(client, csrf),
        }, follow_redirects=False)
        assert r.status_code == 303
        # The gym now shows Active ✓ on Control and the owner's app opens.
        assert "Active ✓" in client.get("/control").text
        assert auth.get("/api/stats").status_code == 200
        assert auth.get("/api/members").status_code == 200
    finally:
        with SessionLocal() as db:
            db.query(models.FaceTemplate).delete()
            db.commit()
        main._admin_face_challenges.clear()


def test_control_activate_without_face_records_nothing(auth, client, admin_on):
    """One rule everywhere (2026-10-23): no face, no unlock. A plain
    form post — no passed face verification — changes NOTHING."""
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402
    before = _db_counts()
    dashboard = _control_login(client)
    r = client.post("/control/activate", data={
        "user_id": _demo_user_id(), "csrf": _csrf_from(dashboard.text),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "face" in r.headers["location"].lower()
    landed = client.get(r.headers["location"]).text
    assert "Face verification is required" in landed
    assert auth.get("/api/stats").status_code == 402  # still locked
    assert _db_counts() == before  # nothing recorded, nothing touched


# ---------------------------------------------------------- switch off ----
def test_switch_off_cycle_via_api(auth, client, admin_on):
    # Seeded subscription is active; the owner is in.
    assert auth.get("/api/stats").status_code == 200
    before = _db_counts()
    assert before["members"] == 48

    r = client.post("/api/admin/switch-off", headers=HEADERS,
                    json={"email": DEMO_EMAIL})
    assert r.status_code == 200, r.text
    assert r.json()["account"]["status"] == "switched_off"

    # Locked at once — even though the paid window is still running.
    plan = auth.get("/api/plan").json()
    assert plan["subscription_active"] is False
    assert plan["subscription_status"] == "switched_off"
    r = auth.get("/api/stats")
    assert r.status_code == 402
    assert r.json()["subscription_status"] == "switched_off"
    assert auth.get("/api/members").status_code == 402

    # NOTHING was deleted.
    assert _db_counts() == before

    # Activate restores it — window still valid, so no second payment.
    r = client.post("/api/admin/activate", headers=HEADERS,
                    json={"email": DEMO_EMAIL})
    assert r.status_code == 200
    assert r.json()["recorded_payment"] is False
    assert r.json()["account"]["status"] == "active"
    assert auth.get("/api/stats").status_code == 200
    assert auth.get("/api/members").status_code == 200
    assert _db_counts() == before  # data intact, totals unchanged


def test_switch_off_from_control_page(auth, client, admin_on):
    dashboard = _control_login(client)
    assert 'action="/control/switch-off"' in dashboard.text
    csrf = _csrf_from(dashboard.text)
    r = client.post("/control/switch-off", data={
        "user_id": _demo_user_id(), "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "Switched Off" in client.get("/control").text
    assert auth.get("/api/stats").status_code == 402
    # And back on from the same page — Activate passes the face scan
    # (stubbed proof), like every activation under the one rule.
    r = client.post("/api/admin/face-template", headers=HEADERS,
                    json={"descriptors": [[0.01] * 128]})
    assert r.status_code == 200, r.text
    try:
        page = client.get("/control").text
        csrf = _csrf_from(page)
        r = client.post("/control/activate", data={
            "user_id": _demo_user_id(), "csrf": csrf,
            **_page_face_proof(client, csrf),
        }, follow_redirects=False)
        assert r.status_code == 303
        assert auth.get("/api/stats").status_code == 200
    finally:
        with SessionLocal() as db:
            db.query(models.FaceTemplate).delete()
            db.commit()
        main._admin_face_challenges.clear()


# ------------------------------------------------------------ providers ----
@pytest.fixture()
def google_on(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _google_signup(client, monkeypatch, email, name):
    """Run the real Google flow with Google's HTTP calls mocked —
    the same simulation the Google auth tests use."""
    monkeypatch.setattr(main, "exchange_code_for_token",
                        lambda code: "fake-access-token")
    monkeypatch.setattr(main, "fetch_google_userinfo", lambda token: {
        "email": email, "email_verified": True, "name": name,
    })
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    r = client.get(
        f"/auth/google/callback?code=x&state={qs['state'][0]}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_google_and_email_providers_reach_control(client, admin_on, google_on, monkeypatch):
    from fastapi.testclient import TestClient

    google_client = TestClient(main.app)
    _google_signup(google_client, monkeypatch,
                   "googleowner@example.in", "Google Gym Owner")

    email_client = TestClient(main.app)
    r = email_client.post("/api/signup", json={
        "owner_name": "Email Owner", "gym_name": "Email Gym",
        "email": "emailowner@example.in", "mobile": "9812345671",
        "password": "secret123",
    })
    assert r.status_code == 200, r.text

    summary = _admin_list(client)
    g = _account(summary, "googleowner@example.in")
    assert g["auth_provider"] == "google"
    assert g["owner_name"] == "Google Gym Owner"
    assert g["joined"] is not None
    e = _account(summary, "emailowner@example.in")
    assert e["auth_provider"] == "email"
    assert e["joined"] is not None

    # …and the badge is on Farhan's page too.
    page = _control_login(client).text
    assert "Google ✓ verified" in page
    assert "Google Gym Owner" in page
