"""Security hardening tests (owner order, 2026-10-23: "make this
website full secure — no one should take the info or hack").

Attack list:
  * Cross-account (IDOR): account B must never see account A's
    payments, pass A's OTP stage with B's code, or spend A's
    face-unlock challenge. Since 2026-10-24 the member register is
    also private per gym (see test_tenant_isolation.py): one install
    serves many gyms, and no gym's data may ever cross into another's.
  * Headers & transport: full header set everywhere, HSTS only over
    HTTPS, Secure cookie flag under X-Forwarded-Proto: https.
  * Leaks: database / backup / log / .env files are not reachable
    over HTTP; /health is minimal; a forced crash answers with a
    generic message, never a stack trace; logs mask emails/phones
    and never contain an OTP outside DEV_OTP_ECHO.
  * Auth strength: bcrypt cost 12; login mints a fresh token each
    time (rotation); logout invalidates ALL previously issued
    tokens (session epoch); login rate limit locks at 5/minute.
  * CSRF: a cross-site Origin on a state-changing API call is
    refused; a forged CSRF token on a Control form changes nothing.
"""
import logging
import re

import pytest
from fastapi.testclient import TestClient

import main
import models
from conftest import ADMIN_HEADERS, admin_activate
from database import SessionLocal

A_EMAIL = "idor-a@test.in"
B_EMAIL = "idor-b@test.in"
PW = "secret6"


def _make_account(client, email):
    """Signup + OTP verify (dev echo) -> a ready, unpaid account."""
    r = client.post("/api/signup", json={
        "owner_name": "Owner " + email[5], "gym_name": "Gym " + email[5].upper(),
        "email": email, "mobile": "9812345678", "password": PW,
    })
    assert r.status_code == 200, r.text
    code = r.json()["dev_otp"]
    r = client.post("/api/auth/otp/verify", json={"code": code})
    assert r.status_code == 200, r.text
    return client


def _fresh_client():
    return TestClient(main.app)


# ------------------------------------------------------------ cross-account ----
def test_account_b_never_sees_account_a_payments():
    a = _make_account(_fresh_client(), A_EMAIL)
    # Farhan collects from A in person and activates A.
    admin_activate(a, email=A_EMAIL, plan_id="pro", period="monthly")
    plan_a = a.get("/api/plan").json()
    assert plan_a["last_payment"]["amount"] == 1899
    assert plan_a["subscription_active"] is True

    b = _make_account(_fresh_client(), B_EMAIL)
    plan_b = b.get("/api/plan").json()
    # B has paid nothing: no last payment, no history, not active —
    # A's ₹1,899 collected_in_person record is nowhere in B's state.
    assert plan_b["subscription_active"] is False
    assert plan_b["last_payment"] is None
    assert plan_b["payments"] == []
    assert b.get("/api/stats").status_code == 402


def test_account_b_cannot_spend_account_a_face_challenge(auth):
    # Enroll Farhan's face; A (the demo account, `auth`) opens a scan.
    r = auth.post("/api/admin/face-template", headers=ADMIN_HEADERS,
                  json={"descriptors": [[0.01] * 128]})
    assert r.status_code == 200
    try:
        ch = auth.post("/api/face-unlock/challenge").json()
        b = _make_account(_fresh_client(), "idor-face-b@test.in")
        r = b.post("/api/face-unlock/verify", json={
            "challenge_id": ch["challenge_id"], "distance": 0.1,
            "liveness": ch["action"],
        })
        assert r.status_code == 400  # the challenge belongs to A alone
        with SessionLocal() as db:
            b_user = db.query(models.User).filter(
                models.User.email == "idor-face-b@test.in").first()
            assert db.query(models.SubscriptionPayment).filter(
                models.SubscriptionPayment.user_id == b_user.id).count() == 0
    finally:
        with SessionLocal() as db:
            db.query(models.FaceTemplate).delete()
            db.commit()
        main._face_challenges.clear()
        main._face_fails.clear()
        main._face_lockouts.clear()


def test_account_b_cannot_pass_otp_with_account_a_code():
    a = _fresh_client()
    r = a.post("/api/signup", json={
        "owner_name": "Owner A2", "gym_name": "Gym A2",
        "email": "idor-otp-a@test.in", "mobile": "9812345678", "password": PW,
    })
    a_code = r.json()["dev_otp"]
    b = _fresh_client()
    r = b.post("/api/signup", json={
        "owner_name": "Owner B2", "gym_name": "Gym B2",
        "email": "idor-otp-b@test.in", "mobile": "9812345678", "password": PW,
    })
    # B enters A's code: hashes are per-account, so it is just wrong.
    r = b.post("/api/auth/otp/verify", json={"code": a_code})
    assert r.status_code == 400
    assert b.get("/api/me").json()["otp_pending"] is True


# ------------------------------------------------------------ headers/transport ----
def test_full_security_header_set(client):
    r = client.get("/")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = h["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    pp = h["permissions-policy"]
    assert "camera=(self)" in pp and "microphone=()" in pp and "geolocation=()" in pp
    # HSTS is NOT sent over plain HTTP (local dev), only over HTTPS.
    assert "strict-transport-security" not in h
    r = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=31536000" in r.headers["strict-transport-security"]


def test_gate_refusals_carry_headers_too(auth):
    with SessionLocal() as db:
        db.query(models.SubscriptionPayment).delete()
        db.commit()
    try:
        r = auth.get("/api/stats", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 402
        assert r.headers["content-security-policy"].startswith("default-src")
        assert "camera=(self)" in r.headers["permissions-policy"]
        assert "max-age=31536000" in r.headers["strict-transport-security"]
    finally:
        admin_activate(auth)


def test_session_cookie_flags_and_secure_under_https(client):
    r = client.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026",
    }, headers={"X-Forwarded-Proto": "https"})
    cookie = r.headers["set-cookie"]
    assert "phg_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" in cookie  # behind HTTPS, the cookie is Secure
    r = client.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026",
    })
    assert "Secure" not in r.headers["set-cookie"]  # plain local HTTP


# --------------------------------------------------------------------- leaks ----
def test_database_backups_logs_and_env_are_not_served(client):
    for path in ("/gym.db", "/backend/gym.db", "/backups/gym-2026-10-23.db",
                 "/.env", "/backend/.env", "/app.log", "/logs/app.log",
                 "/main.py", "/backend/main.py"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"


def test_health_is_minimal(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"status", "database"}


def test_forced_crash_answers_generically(monkeypatch, auth):
    def boom(db, today):
        raise RuntimeError("SELECT * FROM members WHERE secret = 'x'")

    monkeypatch.setattr(main, "red_list", boom)
    quiet = TestClient(main.app, raise_server_exceptions=False)
    quiet.cookies.update(auth.cookies)
    r = quiet.get("/api/stats")
    assert r.status_code == 500
    body = r.text
    assert "Something went wrong" in body
    assert "RuntimeError" not in body and "SELECT" not in body
    assert "Traceback" not in body


def test_logs_mask_emails_and_phones(caplog):
    client = _fresh_client()
    with caplog.at_level(logging.INFO, logger="powerhouse"):
        client.post("/api/login", json={
            "email": "owner@powerhousegym.in", "password": "Gym@2026",
        })
    text = caplog.text
    assert "owner@powerhousegym.in" not in text  # full email never logged
    assert "ow***@powerhousegym.in" in text      # masked form is


def test_otp_code_never_logged_without_dev_echo(client, monkeypatch, caplog):
    monkeypatch.delenv("DEV_OTP_ECHO", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level(logging.INFO, logger="powerhouse"):
        r = client.post("/api/signup", json={
            "owner_name": "Quiet Otp", "gym_name": "Quiet Gym",
            "email": "otp-quiet@test.in", "mobile": "9812345678",
            "password": PW,
        })
    assert r.status_code == 200
    assert "dev_otp" not in r.json()
    otp_lines = [ln for ln in caplog.text.splitlines() if "OTP" in ln]
    assert otp_lines, "the OTP issuance is still logged as an event"
    for ln in otp_lines:
        assert not re.search(r"\b\d{6}\b", ln), f"OTP code leaked into log: {ln}"


# -------------------------------------------------------------- auth strength ----
def test_passwords_use_bcrypt_cost_12():
    c = _fresh_client()
    _make_account(c, "idor-bcrypt@test.in")
    with SessionLocal() as db:
        user = db.query(models.User).filter(
            models.User.email == "idor-bcrypt@test.in").first()
        assert user.password_hash.startswith("$2b$12$")


def test_login_rotates_token_and_logout_invalidates_all_tokens():
    c = _fresh_client()
    r1 = c.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026"})
    token1 = r1.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    r2 = c.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026"})
    token2 = r2.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert token1 != token2  # rotation: a fresh token per login

    assert c.post("/api/logout").status_code == 200
    # Replaying EITHER pre-logout token now fails — the epoch moved.
    for token in (token1, token2):
        thief = _fresh_client()
        thief.cookies.set("phg_session", token)
        assert thief.get("/api/me").status_code == 401
    # And a fresh login works again (new epoch).
    r = c.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026"})
    assert r.status_code == 200
    assert c.get("/api/me").status_code == 200


def test_login_rate_limit_locks(client):
    for i in range(5):
        r = client.post("/api/login", json={
            "email": "owner@powerhousegym.in", "password": "wrong-pass"})
        assert r.status_code == 401
    r = client.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "wrong-pass"})
    assert r.status_code == 429


def test_sql_injection_probes_are_inert(auth):
    r = auth.get("/api/search", params={"q": "' OR '1'='1"})
    assert r.status_code == 200  # normal (filtered) result, no crash
    r = auth.post("/api/login", json={
        "email": "' OR 1=1 --", "password": "x"})
    assert r.status_code in (401, 429)  # simply not a real account


# ----------------------------------------------------------------------- csrf ----
def test_cross_site_origin_on_state_change_is_refused(auth):
    members = auth.get("/api/members").json()["members"]
    target = members[0]["id"]
    r = auth.post("/api/checkin", json={"member_id": target},
                  headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # The same call from the site's own origin goes through.
    r = auth.post("/api/checkin", json={"member_id": target},
                  headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_control_form_with_forged_csrf_changes_nothing(client):
    # Log in to Control properly first.
    page = client.get("/control")
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    r = client.post("/control/login", data={
        "password": "test-admin-secret", "csrf": m.group(1)},
        follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        demo = db.query(models.User).filter(
            models.User.email == "owner@powerhousegym.in").first()
        uid, before = demo.id, db.query(models.SubscriptionPayment).count()
    # A cross-site forged form posts a valid-looking user id but a
    # made-up CSRF token: the activation must NOT happen.
    client.post("/control/activate",
                data={"user_id": str(uid), "csrf": "forged-token"},
                follow_redirects=False)
    with SessionLocal() as db:
        assert db.query(models.SubscriptionPayment).count() == before
