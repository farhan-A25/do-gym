"""Control support actions — Farhan's rescue tools (2026-10-23).

When an owner's emailed OTP/reset code never arrives, email must not
be a hard blocker: Farhan, standing in the gym, can from /control

  * POST /api/control/gyms/{id}/verify-email — clear the account's
    OTP stage (the exact end-state of a successful OTP entry), and
  * POST /api/control/gyms/{id}/temp-password — set a temporary
    password with reset semantics (old sessions die, otp_pending
    untouched).

Both are admin-only exactly like the rest of Control (no ADMIN_SECRET
→ 404; no/bad credentials → 403; cookie path needs its CSRF value),
both are rate-limited, and both write an AdminAction audit row that
never contains a password or a code.
"""
import re

import pytest
from fastapi.testclient import TestClient

import main
import models
from conftest import ADMIN_HEADERS
from database import SessionLocal

CONTROL_SECRET = "test-admin-secret"
DEMO_EMAIL = "owner@powerhousegym.in"
HEADERS = ADMIN_HEADERS


@pytest.fixture()
def admin_on(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", CONTROL_SECRET)


@pytest.fixture()
def admin_off(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)


def _signup(client, email, password="secret6"):
    r = client.post("/api/signup", json={
        "owner_name": "Support Owner", "gym_name": "Support Gym",
        "email": email, "mobile": "9812345678", "password": password,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _user(email):
    with SessionLocal() as db:
        return db.query(models.User).filter(models.User.email == email).first()


def _user_id(email):
    return _user(email).id


def _challenge_row(user_id):
    with SessionLocal() as db:
        return (
            db.query(models.OtpChallenge)
            .filter(models.OtpChallenge.user_id == user_id)
            .first()
        )


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field found in page"
    return m.group(1)


def _control_login(client):
    r = client.get("/control")
    assert r.status_code == 200
    r = client.post("/control/login", data={
        "password": CONTROL_SECRET, "csrf": _csrf_from(r.text),
    })
    assert r.status_code == 200, r.text[:300]
    assert "Total gyms" in r.text  # the dashboard
    return r


# ------------------------------------------------------------ verify-email ----
def test_verify_email_clears_pending_and_opens_the_gate(client, admin_on):
    email = "rescue-verify@test.in"
    _signup(client, email)
    uid = _user_id(email)
    assert _user(email).otp_pending is True
    assert _challenge_row(uid) is not None

    # The OTP gate really is holding this account back.
    r = client.get("/api/details")
    assert r.status_code == 403 and r.json()["code"] == "otp_required", r.text

    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["account"]["otp_pending"] is False

    # Same end-state as a successful OTP entry: flag cleared AND the
    # pending challenge deleted.
    assert _user(email).otp_pending is False
    assert _challenge_row(uid) is None

    # The same session now passes the OTP gate.
    r = client.get("/api/details")
    assert r.status_code == 200, r.text
    assert client.get("/api/me").json()["otp_pending"] is False


def test_verify_email_on_non_pending_account_is_a_clear_400(client, admin_on):
    r = client.post(
        f"/api/control/gyms/{_user_id(DEMO_EMAIL)}/verify-email",
        json={}, headers=HEADERS,
    )
    assert r.status_code == 400, r.text
    assert "already verified" in r.json()["detail"]


def test_verify_email_unknown_gym_is_404(client, admin_on):
    r = client.post("/api/control/gyms/99999999/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 404, r.text


# ------------------------------------------------------------ temp-password ----
def test_temp_password_replaces_password_kills_sessions_keeps_otp(client, admin_on):
    email = "rescue-temp@test.in"
    _signup(client, email, password="oldpass6")
    uid = _user_id(email)
    assert client.get("/api/me").status_code == 200  # signup session live

    r = client.post(f"/api/control/gyms/{uid}/temp-password",
                    json={"password": "temppass9"}, headers=HEADERS)
    assert r.status_code == 200, r.text

    # The old session dies at once (session epoch bumped, reset-style).
    assert client.get("/api/me").status_code == 401

    # The old password no longer works; the temporary one does — and
    # the account STILL owes its OTP: temp passwords never verify.
    # (Fresh client: the dead cookie must not ride along, exactly like
    # the password-reset suite's post-reset logins.)
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", json={"email": email, "password": "oldpass6"})
    assert r.status_code == 401, r.text
    r = fresh.post("/api/login", json={"email": email, "password": "temppass9"})
    assert r.status_code == 200, r.text
    assert r.json()["otp_pending"] is True
    assert fresh.get("/api/me").json()["otp_pending"] is True
    assert _user(email).otp_pending is True


def test_temp_password_on_verified_account_keeps_it_verified(client, admin_on):
    email = "rescue-temp-verified@test.in"
    _signup(client, email, password="oldpass6")
    uid = _user_id(email)
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/control/gyms/{uid}/temp-password",
                    json={"password": "newtemp7"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["account"]["otp_pending"] is False
    r = client.post("/api/login", json={"email": email, "password": "newtemp7"})
    assert r.status_code == 200, r.text
    assert client.get("/api/me").json()["otp_pending"] is False


def test_temp_password_enforces_signup_min_length(client, admin_on):
    email = "rescue-temp-short@test.in"
    _signup(client, email)
    r = client.post(
        f"/api/control/gyms/{_user_id(email)}/temp-password",
        json={"password": "abc"}, headers=HEADERS,
    )
    assert r.status_code == 400, r.text
    assert "at least 6 characters" in r.json()["detail"]
    # Untouched: the original password still logs in (fresh client —
    # this account's live session is still in the OTP stage).
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", json={"email": email, "password": "secret6"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- access ----
def test_support_endpoints_reject_non_admins(client, admin_on):
    uid = _user_id(DEMO_EMAIL)
    for url, payload in (
        (f"/api/control/gyms/{uid}/verify-email", {}),
        (f"/api/control/gyms/{uid}/temp-password", {"password": "temppass9"}),
    ):
        r = client.post(url, json=payload)  # no credentials at all
        assert r.status_code == 403, r.text
        r = client.post(url, json=payload,
                        headers={"X-Admin-Secret": "not-the-secret"})
        assert r.status_code == 403, r.text


def test_support_endpoints_do_not_exist_without_secret(client, admin_off):
    uid = _user_id(DEMO_EMAIL)
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 404, r.text
    r = client.post(f"/api/control/gyms/{uid}/temp-password",
                    json={"password": "temppass9"}, headers=HEADERS)
    assert r.status_code == 404, r.text


def test_support_endpoints_accept_control_cookie_and_require_csrf(client, admin_on):
    email = "rescue-cookie@test.in"
    _signup(client, email)
    uid = _user_id(email)
    dashboard = _control_login(client)
    csrf = _csrf_from(dashboard.text)

    # A forged/missing CSRF value on the cookie path is refused.
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={"csrf": "bogus"})
    assert r.status_code == 403, r.text
    assert _user(email).otp_pending is True

    # The real session cookie + its CSRF value works — no header needed.
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={"csrf": csrf})
    assert r.status_code == 200, r.text
    assert _user(email).otp_pending is False


# ----------------------------------------------------------------- audit ----
def test_support_actions_are_audited_without_secrets(client, admin_on):
    email = "rescue-audit@test.in"
    _signup(client, email)
    uid = _user_id(email)
    temp = "temppass-distinctive-42"
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/control/gyms/{uid}/temp-password",
                    json={"password": temp}, headers=HEADERS)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        rows = (
            db.query(models.AdminAction)
            .filter(models.AdminAction.target_email == email)
            .order_by(models.AdminAction.id)
            .all()
        )
    assert [row.action for row in rows] == ["verify_email", "temp_password"]
    for row in rows:
        assert row.target_user_id == uid
        assert row.created_at is not None
        # The audit trail records THAT it happened — never the secret.
        assert temp not in (row.action + row.target_email + row.detail)


# --------------------------------------------------------------------- UI ----
def test_control_page_shows_support_controls(client, admin_on):
    email = "rescue-ui@test.in"
    _signup(client, email)
    uid = _user_id(email)
    page = _control_login(client).text

    # The pending card offers Verify email (its own form, this gym's
    # id) and every card carries the temp-password control.
    assert f'class="act support-verify" data-user-id="{uid}"' in page
    assert f'class="act support-temp-pw" data-user-id="{uid}"' in page
    assert "Email OTP:" in page and "Set temp password" in page

    # Once verified, the Verify button for this gym is gone.
    r = client.post(f"/api/control/gyms/{uid}/verify-email",
                    json={}, headers=HEADERS)
    assert r.status_code == 200, r.text
    page = client.get("/control").text
    assert f'class="act support-verify" data-user-id="{uid}"' not in page
    assert f'class="act support-temp-pw" data-user-id="{uid}"' in page
