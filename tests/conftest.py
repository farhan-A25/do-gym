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

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DEMO = {"email": "owner@powerhousegym.in", "password": "Gym@2026"}


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
    assert r.status_code == 200, r.text
    return client
