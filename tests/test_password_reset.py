"""Password reset — "Forgot password?" (owner ask, 2026-10-23,
after his own live test account got stuck with a forgotten
password).

POST /api/auth/forgot {email} issues a 6-digit reset code through
the same SMTP path as the signup OTP and ALWAYS answers the same
generic success — no account enumeration. POST /api/auth/reset
{email, code, new_password} checks the code with the signup OTP's
rules (hashed, 10 minutes, 5 attempts), replaces the bcrypt hash,
and bumps the session epoch so every existing session dies.

Purpose separation is structural: reset codes live in their own
table (PasswordResetChallenge) and are hashed with a "pwreset:"
domain prefix — a signup OTP can never reset a password, and a
reset code can never verify an email.

DEV_OTP_ECHO=1 is set by conftest, so issued codes sit in the
challenge row's dev_code (read straight from the DB here, the same
pattern test_otp.py uses) — the forgot response itself never
carries a code, in any mode.
"""
from datetime import datetime, timedelta

import main
import models
from database import SessionLocal
from fastapi.testclient import TestClient

GENERIC = {
    "ok": True,
    "message": "If an account with this email exists, a reset code has been sent.",
}


def _signup(client, email, password="secret6"):
    r = client.post("/api/signup", json={
        "owner_name": "Reset Owner", "gym_name": "Reset Gym", "email": email,
        "mobile": "9812345678", "password": password,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _verify_signup_otp(client, code):
    r = client.post("/api/auth/otp/verify", json={"code": code})
    assert r.status_code == 200, r.text


def _user(email):
    with SessionLocal() as db:
        return db.query(models.User).filter(models.User.email == email).first()


def _reset_row(email):
    user = _user(email)
    with SessionLocal() as db:
        return (
            db.query(models.PasswordResetChallenge)
            .filter(models.PasswordResetChallenge.user_id == user.id)
            .first()
        )


def _forgot(client, email):
    r = client.post("/api/auth/forgot", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()


def _reset(client, email, code, new_password):
    return client.post("/api/auth/reset", json={
        "email": email, "code": code, "new_password": new_password,
    })


def _wrong_code(real):
    return "111111" if real != "111111" else "222222"


def _backdate_reset(email, *, expired=False):
    user = _user(email)
    with SessionLocal() as db:
        row = (
            db.query(models.PasswordResetChallenge)
            .filter(models.PasswordResetChallenge.user_id == user.id)
            .first()
        )
        if expired:
            row.expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()


# ------------------------------------------------------------------ flow ----
def test_forgot_issues_code_and_reset_changes_password(client):
    data = _signup(client, "reset-flow@test.in", password="oldpass6")
    _verify_signup_otp(client, data["dev_otp"])
    client.post("/api/logout")

    body = _forgot(client, "reset-flow@test.in")
    assert body == GENERIC  # the response never carries the code
    row = _reset_row("reset-flow@test.in")
    assert row is not None
    code = row.dev_code
    assert code and len(code) == 6 and code.isdigit()
    assert row.code_hash != code and len(row.code_hash) == 64  # hashed

    r = _reset(client, "reset-flow@test.in", code, "newpass6")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _reset_row("reset-flow@test.in") is None  # code consumed

    # Old password is dead, the new one logs in.
    r = client.post("/api/login", json={
        "email": "reset-flow@test.in", "password": "oldpass6",
    })
    assert r.status_code == 401
    r = client.post("/api/login", json={
        "email": "reset-flow@test.in", "password": "newpass6",
    })
    assert r.status_code == 200, r.text

    # The spent code cannot be replayed.
    r = _reset(client, "reset-flow@test.in", code, "another6")
    assert r.status_code == 400 and r.json()["code"] == "reset_invalid"


def test_reset_kills_existing_sessions(client):
    data = _signup(client, "reset-session@test.in", password="oldpass6")
    _verify_signup_otp(client, data["dev_otp"])
    assert client.get("/api/me").status_code == 200  # session alive

    # The reset runs from a different, logged-out browser.
    anon = TestClient(main.app)
    _forgot(anon, "reset-session@test.in")
    code = _reset_row("reset-session@test.in").dev_code
    r = _reset(anon, "reset-session@test.in", code, "newpass6")
    assert r.status_code == 200

    # The pre-reset cookie is now worthless (session epoch bumped).
    assert client.get("/api/me").status_code == 401


def test_forgot_unknown_email_identical_and_sends_nothing(client):
    _signup(client, "reset-known@test.in")
    known = _forgot(client, "reset-known@test.in")
    with SessionLocal() as db:
        before = db.query(models.PasswordResetChallenge).count()
    unknown = _forgot(client, "reset-nobody@test.in")
    assert unknown == known == GENERIC  # indistinguishable
    with SessionLocal() as db:
        after = db.query(models.PasswordResetChallenge).count()
    assert after == before  # nothing issued, nothing sent


def test_forgot_resend_within_cooldown_keeps_first_code(client):
    _signup(client, "reset-cooldown@test.in")
    _forgot(client, "reset-cooldown@test.in")
    first = _reset_row("reset-cooldown@test.in")
    _forgot(client, "reset-cooldown@test.in")  # inside the 60s cooldown
    second = _reset_row("reset-cooldown@test.in")
    assert second.code_hash == first.code_hash  # not re-issued
    assert second.dev_code == first.dev_code
    r = _reset(client, "reset-cooldown@test.in", first.dev_code, "newpass6")
    assert r.status_code == 200


def test_wrong_codes_count_attempts_and_lock(client):
    _signup(client, "reset-lock@test.in")
    _forgot(client, "reset-lock@test.in")
    row = _reset_row("reset-lock@test.in")
    wrong = _wrong_code(row.dev_code)
    for _ in range(2):
        r = _reset(client, "reset-lock@test.in", wrong, "newpass6")
        assert r.status_code == 400 and r.json()["code"] == "reset_invalid"
    assert _reset_row("reset-lock@test.in").attempts == 2
    for _ in range(3):
        r = _reset(client, "reset-lock@test.in", wrong, "newpass6")
        assert r.status_code == 400
    assert _reset_row("reset-lock@test.in").attempts == 5
    # Spent: even the right code is refused now. (Clear the per-IP
    # limiter bucket first — conftest does the same between tests —
    # so this assertion exercises the attempt cap, not the limiter.)
    main._login_attempts.clear()
    r = _reset(client, "reset-lock@test.in", row.dev_code, "newpass6")
    assert r.status_code == 400 and r.json()["code"] == "reset_invalid"
    # And the password never changed along the way. (Logout first:
    # the pending-OTP session would gate even the login call — the
    # same pattern test_otp.py uses.)
    client.post("/api/logout")
    r = client.post("/api/login", json={
        "email": "reset-lock@test.in", "password": "secret6",
    })
    assert r.status_code == 200


def test_expired_reset_code_fails(client):
    _signup(client, "reset-expired@test.in", password="oldpass6")
    _forgot(client, "reset-expired@test.in")
    code = _reset_row("reset-expired@test.in").dev_code
    _backdate_reset("reset-expired@test.in", expired=True)
    r = _reset(client, "reset-expired@test.in", code, "newpass6")
    assert r.status_code == 400 and r.json()["code"] == "reset_invalid"
    client.post("/api/logout")  # pending-OTP session gates login itself
    r = client.post("/api/login", json={
        "email": "reset-expired@test.in", "password": "oldpass6",
    })
    assert r.status_code == 200  # untouched


def test_short_new_password_rejected_before_code_is_spent(client):
    _signup(client, "reset-shortpw@test.in")
    _forgot(client, "reset-shortpw@test.in")
    row = _reset_row("reset-shortpw@test.in")
    r = _reset(client, "reset-shortpw@test.in", row.dev_code, "abc12")
    assert r.status_code == 400
    assert "new_password" in r.json()["fields"]  # same rule as signup (min 6)
    assert _reset_row("reset-shortpw@test.in").attempts == 0  # no attempt burned
    r = _reset(client, "reset-shortpw@test.in", row.dev_code, "valid66")
    assert r.status_code == 200


# ------------------------------------------------------- purpose separation ----
def test_signup_otp_cannot_reset_and_reset_code_cannot_verify(client):
    data = _signup(client, "reset-purpose@test.in", password="oldpass6")
    signup_code = data["dev_otp"]

    # The signup OTP is not a reset code (no reset was ever asked for).
    r = _reset(client, "reset-purpose@test.in", signup_code, "newpass6")
    assert r.status_code == 400 and r.json()["code"] == "reset_invalid"

    # Ask for a reset, then try to spend the reset code as the
    # signup OTP: refused by the OTP endpoint.
    _forgot(client, "reset-purpose@test.in")
    reset_code = _reset_row("reset-purpose@test.in").dev_code
    if reset_code != signup_code:  # (identical digits would BE the signup code)
        r = client.post("/api/auth/otp/verify", json={"code": reset_code})
        assert r.status_code == 400

    # The separation is in the hashes themselves: even fed the signup
    # code, the reset hasher can never reproduce the signup hash.
    user = _user("reset-purpose@test.in")
    with SessionLocal() as db:
        signup_row = (
            db.query(models.OtpChallenge)
            .filter(models.OtpChallenge.user_id == user.id)
            .first()
        )
    assert main._reset_hash(user, signup_code) != signup_row.code_hash


def test_reset_does_not_clear_otp_pending(client):
    data = _signup(client, "reset-pending@test.in", password="oldpass6")
    assert data["otp_pending"] is True
    _forgot(client, "reset-pending@test.in")
    code = _reset_row("reset-pending@test.in").dev_code
    r = _reset(client, "reset-pending@test.in", code, "newpass6")
    assert r.status_code == 200

    # Login works with the NEW password — and still lands on the
    # signup-OTP stage: a password reset is not an email verification.
    client.post("/api/logout")
    r = client.post("/api/login", json={
        "email": "reset-pending@test.in", "password": "oldpass6",
    })
    assert r.status_code == 401
    r = client.post("/api/login", json={
        "email": "reset-pending@test.in", "password": "newpass6",
    })
    assert r.status_code == 200 and r.json()["otp_pending"] is True
    assert client.get("/api/me").json()["otp_pending"] is True


def test_forgot_reachable_while_logged_in_and_unpaid(client):
    # Password recovery is never paywalled: a signed-in owner whose
    # subscription is inactive still reaches both endpoints.
    data = _signup(client, "reset-gate@test.in")
    _verify_signup_otp(client, data["dev_otp"])
    body = _forgot(client, "reset-gate@test.in")
    assert body == GENERIC
    assert _reset_row("reset-gate@test.in") is not None
