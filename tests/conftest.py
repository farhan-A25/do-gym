"""Pytest suite for the PowerHouse Gym Owner App backend.

Run from the gym-app folder:
    python -m pytest tests/ -q

Uses a throwaway SQLite database (DATABASE_URL points at a temp file), so
the real gym.db is never touched.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="phg-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test_gym.db"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_SECRET", "test-admin-secret")
# Dev-only OTP echo: signup/login/otp-send responses carry the live
# code so tests can complete the canonical OTP stage without SMTP.
# Production never sets this (README: keep DEV_OTP_ECHO off live).
os.environ.setdefault("DEV_OTP_ECHO", "1")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DEMO = {"email": "owner@powerhousegym.in", "password": "Gym@2026"}

ADMIN_HEADERS = {"X-Admin-Secret": os.environ["ADMIN_SECRET"]}


def admin_activate(client, email=DEMO["email"], plan_id="pro", period="monthly"):
    """Activate a gym the way Farhan does after collecting in person
    (the ADMIN_SECRET API). Returns the response JSON."""
    r = client.post(
        "/api/admin/activate",
        json={"email": email, "plan_id": plan_id, "period": period},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    main._login_attempts.clear()
    yield
    main._login_attempts.clear()


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def auth(client):
    """Logged-in client with freshly reset demo data."""
    r = client.post("/api/login", json=DEMO)
    assert r.status_code == 200, r.text
    r = client.post("/api/reset")
    if r.status_code == 402:
        # A previous test left the subscription unpaid — and /api/reset
        # is itself behind the pay-first gate (by design). Farhan's
        # admin activation re-opens the gate, then the reset runs.
        admin_activate(client)
        r = client.post("/api/reset")
    assert r.status_code == 200, r.text
    return client
