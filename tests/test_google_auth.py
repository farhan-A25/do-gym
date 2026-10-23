"""Google sign-in (OAuth) tests.

Two groups:
  * Disabled mode (no GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET): the config
    endpoint reports google_enabled=false and /auth/google answers with a
    friendly redirect instead of crashing.
  * Enabled mode: the Google HTTP calls are mocked (no network), and the
    callback must log in an existing user, create + log in a new user at
    the details stage of the entry flow, and refuse unverified emails and
    bad state values — always with friendly redirects, never a 500.
"""
import urllib.parse

import pytest

import main
import models
from database import SessionLocal


@pytest.fixture()
def google_on(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture()
def google_off(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)


def _start_state(client) -> str:
    """Run GET /auth/google and pull the signed state out of the redirect."""
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    return qs["state"][0]


def _mock_google(monkeypatch, userinfo: dict):
    monkeypatch.setattr(main, "exchange_code_for_token", lambda code: "fake-access-token")
    monkeypatch.setattr(main, "fetch_google_userinfo", lambda token: userinfo)


def _user_count(email: str) -> int:
    with SessionLocal() as db:
        return db.query(models.User).filter(models.User.email == email).count()


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------
def test_auth_config_reports_disabled_without_keys(client, google_off):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {"google_enabled": False, "mail_configured": False}


def test_google_start_disabled_is_a_friendly_redirect(client, google_off):
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")


def test_google_callback_disabled_is_a_friendly_redirect(client, google_off):
    r = client.get("/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")


# ---------------------------------------------------------------------------
# Enabled mode — start of flow
# ---------------------------------------------------------------------------
def test_auth_config_reports_enabled_with_keys(client, google_on):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {"google_enabled": True, "mail_configured": False}


def test_google_start_redirects_to_google_consent(client, google_on):
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    parsed = urllib.parse.urlparse(r.headers["location"])
    assert parsed.netloc == "accounts.google.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["client_id"] == ["test-client-id"]
    assert qs["redirect_uri"] == ["http://testserver/auth/google/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["openid email profile"]
    assert qs["state"]  # signed state present


# ---------------------------------------------------------------------------
# Enabled mode — callback (Google mocked)
# ---------------------------------------------------------------------------
def test_callback_creates_new_user_and_enters_details_stage(client, google_on, monkeypatch):
    state = _start_state(client)
    _mock_google(monkeypatch, {
        "email": "NewGym@Example.com", "email_verified": True,
        "name": "Priya Nair", "picture": "https://example.com/p.png",
    })
    r = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # The session cookie was set on the redirect: /api/me now works and the
    # brand-new account sits at the start of the entry flow (details next).
    me = client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "newgym@example.com"  # stored lower-cased
    assert body["owner_name"] == "Priya Nair"
    assert body["otp_pending"] is False  # Google's verified email verifies
    assert body["details_completed"] is False
    assert body["plan_chosen"] is False
    assert _user_count("newgym@example.com") == 1


def test_callback_logs_in_existing_user_without_duplicating(client, google_on, monkeypatch):
    state = _start_state(client)
    _mock_google(monkeypatch, {
        "email": "owner@powerhousegym.in", "email_verified": True, "name": "Demo Owner",
    })
    r = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@powerhousegym.in"
    assert me.json()["details_completed"] is True  # existing account untouched
    assert _user_count("owner@powerhousegym.in") == 1


def test_callback_rejects_unverified_email(client, google_on, monkeypatch):
    state = _start_state(client)
    _mock_google(monkeypatch, {
        "email": "sneaky@example.com", "email_verified": False, "name": "Sneaky",
    })
    r = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")
    assert _user_count("sneaky@example.com") == 0


def test_callback_rejects_bad_state(client, google_on, monkeypatch):
    _mock_google(monkeypatch, {
        "email": "x@example.com", "email_verified": True, "name": "X",
    })
    r = client.get(
        "/auth/google/callback?code=abc&state=not-a-real-state",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")
    assert _user_count("x@example.com") == 0


def test_callback_google_failure_is_friendly_not_500(client, google_on, monkeypatch):
    state = _start_state(client)

    def boom(code):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "exchange_code_for_token", boom)
    r = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")


def test_callback_cancelled_by_user_is_friendly(client, google_on):
    r = client.get(
        "/auth/google/callback?error=access_denied&state=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/?auth_error=")
