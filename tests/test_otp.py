"""Email OTP — stage 2 of the email/password signup flow:
(1) account created by email/password, (2) OTP generated and sent
to that email, owner enters it, (3) details, (4) payment,
(5) animated unlock, (6) the app. The Google route skips this stage
entirely (owner's new order, superseding Google -> OTP -> details):
Google sign-in verifies by itself — see the google section below.

Enforcement is server-side: while otp_pending, details/payment/app
are all 403 (code otp_required). Codes are stored hashed, live 10
minutes, allow 5 attempts, resend after a 60-second cooldown.
DEV_OTP_ECHO=1 is set by conftest (dev/tests only — production keeps
it off and codes arrive by SMTP email instead).
"""
import urllib.parse
from datetime import datetime, timedelta

import pytest

import main
import models
from database import SessionLocal

GOOGLE_USER = {
    "email": "otp-google@test.in",
    "email_verified": True,
    "name": "Google Otp Owner",
}


def _signup(client, email):
    r = client.post("/api/signup", json={
        "owner_name": "Otp Owner", "gym_name": "OTP Gym", "email": email,
        "mobile": "9812345678", "password": "secret6",
    })
    assert r.status_code == 200, r.text
    return r.json()


def _user(email):
    with SessionLocal() as db:
        return db.query(models.User).filter(models.User.email == email).first()


def _challenge_row(email):
    user = _user(email)
    with SessionLocal() as db:
        return (
            db.query(models.OtpChallenge)
            .filter(models.OtpChallenge.user_id == user.id)
            .first()
        )


def _backdate(email, *, sent_seconds_ago=None, expired=False):
    user = _user(email)
    with SessionLocal() as db:
        row = (
            db.query(models.OtpChallenge)
            .filter(models.OtpChallenge.user_id == user.id)
            .first()
        )
        if sent_seconds_ago is not None:
            row.last_sent_at = datetime.now() - timedelta(seconds=sent_seconds_ago)
        if expired:
            row.expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()


def _wrong_code(real):
    return "111111" if real != "111111" else "222222"


DETAILS = {
    "gym_name": "OTP Gym", "gym_address": "Road 1", "gym_city": "Warangal",
    "gym_phone": "9812345670", "opening_year": 2021,
    "owner_name": "Otp Owner", "mobile": "9812345678", "whatsapp": "",
}


# ------------------------------------------------------------------ email ----
def test_signup_lands_in_otp_stage_and_blocks_everything(client):
    data = _signup(client, "otp-flow@test.in")
    assert data["otp_pending"] is True
    code = data["dev_otp"]
    assert len(code) == 6 and code.isdigit()

    me = client.get("/api/me").json()
    assert me["otp_pending"] is True

    # Details, plan and the app itself are ALL refused before the OTP.
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 403 and r.json()["code"] == "otp_required"
    r = client.get("/api/plan")
    assert r.status_code == 403 and r.json()["code"] == "otp_required"
    r = client.get("/api/stats")
    assert r.status_code == 403 and r.json()["code"] == "otp_required"

    # A wrong code is refused; the right one opens the next stage.
    r = client.post("/api/auth/otp/verify", json={"code": _wrong_code(code)})
    assert r.status_code == 400 and r.json()["code"] == "otp_wrong"
    r = client.post("/api/auth/otp/verify", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json()["otp_pending"] is False
    assert client.get("/api/me").json()["otp_pending"] is False

    # Details now save (the payment gate is the NEXT stage, and the
    # details step itself is part of the entry flow).
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 200, r.text


def test_code_is_stored_hashed_not_plain(client):
    data = _signup(client, "otp-hash@test.in")
    row = _challenge_row("otp-hash@test.in")
    assert row is not None
    assert row.code_hash != data["dev_otp"]
    assert len(row.code_hash) == 64  # sha256 hex
    # dev_code exists ONLY because the dev echo is on in tests.
    assert row.dev_code == data["dev_otp"]


def test_five_wrong_codes_lock_and_resend_recovers(client):
    data = _signup(client, "otp-lock@test.in")
    wrong = _wrong_code(data["dev_otp"])
    for i in range(5):
        r = client.post("/api/auth/otp/verify", json={"code": wrong})
        assert r.status_code == 400
    assert r.json()["code"] == "otp_locked"
    # Even the right code is refused now — the challenge is spent.
    r = client.post("/api/auth/otp/verify", json={"code": data["dev_otp"]})
    assert r.status_code == 400 and r.json()["code"] == "otp_locked"
    # A resend (after the cooldown) issues a fresh, working code.
    _backdate("otp-lock@test.in", sent_seconds_ago=61)
    r = client.post("/api/auth/otp/send")
    assert r.status_code == 200
    fresh = r.json()["dev_otp"]
    r = client.post("/api/auth/otp/verify", json={"code": fresh})
    assert r.status_code == 200


def test_expired_code_is_refused(client):
    data = _signup(client, "otp-expired@test.in")
    _backdate("otp-expired@test.in", expired=True)
    r = client.post("/api/auth/otp/verify", json={"code": data["dev_otp"]})
    assert r.status_code == 400 and r.json()["code"] == "otp_expired"
    assert client.get("/api/me").json()["otp_pending"] is True


def test_resend_cooldown_and_production_echo_off(client, monkeypatch):
    data = _signup(client, "otp-cooldown@test.in")
    # Dev echo ON: a resend inside the cooldown returns the live code
    # (the test/local-dev backdoor — this is its only job).
    r = client.post("/api/auth/otp/send")
    assert r.status_code == 200 and r.json()["dev_otp"] == data["dev_otp"]
    # Production (echo OFF): the same resend is a real 429, and no
    # response ever carries a code.
    monkeypatch.delenv("DEV_OTP_ECHO", raising=False)
    r = client.post("/api/auth/otp/send")
    assert r.status_code == 429 and r.json()["code"] == "otp_cooldown"
    assert "dev_otp" not in r.json()
    _backdate("otp-cooldown@test.in", sent_seconds_ago=61)
    r = client.post("/api/auth/otp/send")
    assert r.status_code == 200 and "dev_otp" not in r.json()


def test_login_reissues_when_code_expired(client):
    data = _signup(client, "otp-relogin@test.in")
    _backdate("otp-relogin@test.in", expired=True, sent_seconds_ago=3600)
    client.post("/api/logout")
    r = client.post("/api/login", json={
        "email": "otp-relogin@test.in", "password": "secret6",
    })
    assert r.status_code == 200
    assert r.json()["otp_pending"] is True
    fresh = r.json()["dev_otp"]  # a fresh code was issued at login
    r = client.post("/api/auth/otp/verify", json={"code": fresh})
    assert r.status_code == 200


def test_demo_account_is_never_otp_pending(client):
    r = client.post("/api/login", json={
        "email": "owner@powerhousegym.in", "password": "Gym@2026",
    })
    assert r.status_code == 200
    assert r.json()["otp_pending"] is False
    assert client.get("/api/me").json()["otp_pending"] is False


# ----------------------------------------------------------------- google ----
@pytest.fixture()
def google_on(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setattr(main, "exchange_code_for_token", lambda code: "fake-token")
    monkeypatch.setattr(main, "fetch_google_userinfo", lambda token: GOOGLE_USER)


def _google_callback(client):
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    state = qs["state"][0]
    r = client.get(
        f"/auth/google/callback?code=fake&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return r


def test_google_new_account_is_verified_by_google_no_otp(client, google_on):
    _google_callback(client)
    me = client.get("/api/me").json()
    # Google's verified email IS the verification (owner's new order):
    # the account is born verified — no OTP stage, no challenge row.
    assert me["otp_pending"] is False
    assert me["email"] == GOOGLE_USER["email"]
    assert _challenge_row(GOOGLE_USER["email"]) is None

    # The OTP gate is already passed: details save straight away.
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 200, r.text


def test_google_returning_user_stays_verified_no_otp(client, google_on, monkeypatch):
    monkeypatch.setattr(main, "fetch_google_userinfo", lambda token: {
        "email": "otp-google-returning@test.in",
        "email_verified": True,
        "name": "Returning Owner",
    })
    _google_callback(client)
    assert client.get("/api/me").json()["otp_pending"] is False
    client.post("/api/logout")
    _google_callback(client)  # same Google account again
    me = client.get("/api/me").json()
    assert me["otp_pending"] is False  # verified by Google — no OTP, ever


def test_google_signin_verifies_pending_email_account(client, google_on, monkeypatch):
    # An email signup whose code never arrived sits otp_pending with a
    # live challenge; signing in with Google for the SAME email proves
    # inbox ownership and clears the stage on the spot.
    email = "otp-pending-then-google@test.in"
    data = _signup(client, email)
    assert data["otp_pending"] is True
    assert _challenge_row(email) is not None
    client.post("/api/logout")

    monkeypatch.setattr(main, "fetch_google_userinfo", lambda token: {
        "email": email, "email_verified": True, "name": "Otp Owner",
    })
    _google_callback(client)

    me = client.get("/api/me").json()
    assert me["otp_pending"] is False
    assert _challenge_row(email) is None  # pending challenge deleted
    # ... and the account passes the OTP gate:
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 200, r.text


def test_email_signup_still_goes_through_otp(client):
    # Regression: the email/password route is unchanged by the Google
    # change — signup still lands in the OTP stage with a live code
    # outstanding and the gate shut.
    data = _signup(client, "otp-email-regression@test.in")
    assert data["otp_pending"] is True
    assert _challenge_row("otp-email-regression@test.in") is not None
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 403 and r.json()["code"] == "otp_required"
