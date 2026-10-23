"""Face unlock (owner's final unlock decision, 2026-10-23).

Covers: enrollment storage/replacement + validation, the
/api/plan face_unlock_available flag that drives the (runtime-injected)
button, the challenge lifecycle (issue / expiry / single-use), a
stubbed successful verification recording method "face_unlock" and
lifting the gate, failed-attempt lockout, the switch-off override,
and local-only asset serving. No camera exists in CI — the browser
match is stubbed at the verify call, exactly the seam the server
enforces (challenge + liveness + distance threshold).
"""
import os
import re
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import main
import models
from conftest import ADMIN_HEADERS, admin_activate
from database import SessionLocal

POSE_A = [0.01] * 128
POSE_B = [0.02] * 128
SECRET = os.environ["ADMIN_SECRET"]
DEMO_EMAIL = "owner@powerhousegym.in"


@pytest.fixture(autouse=True)
def _face_cleanup():
    yield
    with SessionLocal() as db:
        db.query(models.FaceTemplate).delete()
        db.query(models.Setting).filter(
            models.Setting.key.like("revoked_user_%")
        ).delete(synchronize_session=False)
        db.commit()
    main._face_challenges.clear()
    main._face_fails.clear()
    main._face_lockouts.clear()
    main._admin_face_challenges.clear()


@pytest.fixture(autouse=True)
def _leave_active(auth):
    yield
    admin_activate(auth)


def _enroll(client, poses=(POSE_A, POSE_B, POSE_A)):
    r = client.post(
        "/api/admin/face-template", headers=ADMIN_HEADERS,
        json={"descriptors": [list(p) for p in poses]},
    )
    assert r.status_code == 200, r.text
    return r


def _templates():
    with SessionLocal() as db:
        return db.query(models.FaceTemplate).all()


def _make_unpaid():
    with SessionLocal() as db:
        db.query(models.SubscriptionPayment).delete()
        for key in ("plan_status", "payment_status"):
            row = db.query(models.Setting).filter(models.Setting.key == key).first()
            if row:
                row.value = ""
        db.commit()


def _challenge(client):
    r = client.post("/api/face-unlock/challenge")
    assert r.status_code == 200, r.text
    return r.json()


def _verify(client, challenge, distance=0.31, liveness=None):
    return client.post(
        "/api/face-unlock/verify",
        json={
            "challenge_id": challenge["challenge_id"],
            "distance": distance,
            "liveness": challenge["action"] if liveness is None else liveness,
        },
    )


def _control_login(client):
    page = client.get("/control")
    assert page.status_code == 200
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m, "login form carries a csrf token"
    r = client.post(
        "/control/login",
        data={"password": SECRET, "csrf": m.group(1)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


# ------------------------------------------------------------- enrollment ----
def test_enroll_stores_descriptors_and_replaces(client):
    r = _enroll(client)
    assert r.json()["poses"] == 3
    rows = _templates()
    assert len(rows) == 1
    import json as _json
    stored = _json.loads(rows[0].descriptors)
    assert stored == [[0.01] * 128, [0.02] * 128, [0.01] * 128]
    # The record holds numbers only — no image-ish payload anywhere.
    assert "data:" not in rows[0].descriptors and "base64" not in rows[0].descriptors

    # Re-registering replaces the single template.
    _enroll(client, poses=(POSE_B,))
    rows = _templates()
    assert len(rows) == 1
    assert _json.loads(rows[0].descriptors) == [[0.02] * 128]


def test_enroll_requires_admin(client):
    r = client.post("/api/admin/face-template",
                    json={"descriptors": [POSE_A]})
    assert r.status_code == 403
    r = client.post("/api/admin/face-template",
                    headers={"X-Admin-Secret": "wrong"},
                    json={"descriptors": [POSE_A]})
    assert r.status_code == 403
    assert _templates() == []


def test_enroll_validation(client):
    r = client.post("/api/admin/face-template", headers=ADMIN_HEADERS,
                    json={"descriptors": []})
    assert r.status_code == 400
    r = client.post("/api/admin/face-template", headers=ADMIN_HEADERS,
                    json={"descriptors": [[0.1] * 64]})  # wrong length
    assert r.status_code == 400
    r = client.post("/api/admin/face-template", headers=ADMIN_HEADERS,
                    json={"descriptors": [POSE_A] * 7})  # too many poses
    assert r.status_code == 400
    assert _templates() == []


def test_remove_template_hides_face_unlock(client, auth):
    _enroll(client)
    assert auth.get("/api/plan").json()["face_unlock_available"] is True
    r = client.post("/api/admin/face-template/remove", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert _templates() == []
    assert auth.get("/api/plan").json()["face_unlock_available"] is False


# ------------------------------------------------------------- availability ----
def test_plan_flag_and_template_endpoint(auth, client):
    assert auth.get("/api/plan").json()["face_unlock_available"] is False
    t = auth.get("/api/face-unlock/template").json()
    assert t == {"enrolled": False, "descriptors": []}

    _enroll(client)
    assert auth.get("/api/plan").json()["face_unlock_available"] is True
    t = auth.get("/api/face-unlock/template").json()
    assert t["enrolled"] is True
    assert len(t["descriptors"]) == 3


def test_no_unlock_button_is_pre_rendered_on_upgrade(client):
    """The upgrade screen itself carries no unlock action at all —
    the ONLY owner-screen unlock is the payment panel's Pay button
    (see test_panel_pay_button_is_the_only_unlock_path)."""
    page = client.get("/").text
    assert 'id="face-unlock-slot"' not in page
    _enroll(client)
    page = client.get("/").text
    assert 'id="face-unlock-slot"' not in page


def test_challenge_needs_enrollment_and_login(client, auth):
    anon = TestClient(main.app)
    assert anon.post("/api/face-unlock/challenge").status_code == 401
    assert auth.post("/api/face-unlock/challenge").status_code == 400  # not enrolled
    _enroll(client)
    ch = _challenge(auth)
    assert ch["action"] in ("blink", "turn_left", "turn_right")
    assert ch["expires_in"] == 120
    assert ch["threshold"] == 0.5


# ----------------------------------------------------------------- verify ----
def test_successful_verify_records_face_unlock_and_lifts_gate(auth, client):
    _make_unpaid()
    assert auth.get("/api/stats").status_code == 402
    _enroll(client)

    r = _verify(auth, _challenge(auth))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subscription_active"] is True
    assert body["last_payment"]["method"] == "face_unlock"
    # The gym's chosen plan (seeded: Premium monthly, ₹1,899).
    assert body["last_payment"]["amount"] == 1899
    assert auth.get("/api/stats").status_code == 200

    with SessionLocal() as db:
        pay = (
            db.query(models.SubscriptionPayment)
            .order_by(models.SubscriptionPayment.id.desc())
            .first()
        )
        assert pay.method == "face_unlock" and pay.status == "paid"


def test_verify_rejects_bad_distance_and_wrong_liveness(auth, client):
    _make_unpaid()
    _enroll(client)
    r = _verify(auth, _challenge(auth), distance=0.9)
    assert r.status_code == 400
    r = _verify(auth, _challenge(auth), liveness="nod")
    assert r.status_code == 400
    assert auth.get("/api/stats").status_code == 402  # still gated


def test_challenge_is_single_use(auth, client):
    _make_unpaid()
    _enroll(client)
    ch = _challenge(auth)
    assert _verify(auth, ch).status_code == 200
    with SessionLocal() as db:
        count = db.query(models.SubscriptionPayment).count()
    r = _verify(auth, ch)  # same challenge again
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.query(models.SubscriptionPayment).count() == count


def test_expired_challenge_is_refused(auth, client):
    _make_unpaid()
    _enroll(client)
    ch = _challenge(auth)
    main._face_challenges[ch["challenge_id"]]["expires"] = time.time() - 1
    assert _verify(auth, ch).status_code == 400
    assert auth.get("/api/stats").status_code == 402


def test_five_failures_lock_face_unlock_for_15_minutes(auth, client):
    _make_unpaid()
    _enroll(client)
    for _ in range(5):
        assert _verify(auth, _challenge(auth), distance=0.95).status_code == 400
    # Locked: no new challenges, and even a perfect claim is refused.
    assert auth.post("/api/face-unlock/challenge").status_code == 429
    ch = {"challenge_id": "whatever-1234", "action": "blink"}
    assert _verify(auth, ch).status_code == 429
    with SessionLocal() as db:
        assert db.query(models.SubscriptionPayment).count() == 0


def test_switch_off_overrides_face_unlock(auth, client):
    _enroll(client)
    admin_activate(auth)  # active via collection
    assert auth.get("/api/stats").status_code == 200
    r = client.post("/api/admin/switch-off", headers=ADMIN_HEADERS,
                    json={"email": DEMO_EMAIL})
    assert r.status_code == 200
    assert auth.get("/api/stats").status_code == 402
    r = _verify(auth, _challenge(auth))
    assert r.status_code == 403  # switched off — a scan cannot override
    assert auth.get("/api/stats").status_code == 402


# ------------------------------------------------------------- assets/page ----
def test_face_assets_are_served_locally_and_safely(client):
    r = client.get("/static/face/face-api.min.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    r = client.get("/static/face/tiny_face_detector_model-weights_manifest.json")
    assert r.status_code == 200
    # Nothing outside the face folder is reachable through this route.
    assert client.get("/static/face/payment-qr.png").status_code == 404
    assert client.get("/static/face/nonexistent.bin").status_code == 404


def test_control_page_shows_face_card(client):
    _control_login(client)
    page = client.get("/control").text
    assert "Your face — gym unlock" in page
    assert "Not registered" in page
    assert 'id="face-reg-btn"' in page
    # Activate verifies on the DEDICATED scan page (same flow order
    # as the owner's): the page markup ships hidden alongside the
    # dashboard, and the in-card scan box is gone.
    assert 'id="ctl-scan-page"' in page
    assert "Scan Your Face" in page
    assert 'id="ctl-scan"' not in page
    _enroll(client)
    page = client.get("/control").text
    assert "Registered ✓" in page
    assert "3 pose(s) on file" in page


# -------------------------------------------- face-gated activation (§20) ----
def _admin_csrf(client):
    page = client.get("/control")
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m
    return m.group(1)


def test_cookie_api_activation_requires_face_proof(auth, client):
    """One rule everywhere: even Farhan's own browser session cannot
    activate without a passed face verification."""
    _make_unpaid()
    _enroll(client)
    _control_login(client)
    csrf = _admin_csrf(client)
    r = client.post("/api/admin/activate",
                    json={"email": DEMO_EMAIL, "csrf": csrf})
    assert r.status_code == 403
    assert r.json()["code"] == "face_required"
    assert auth.get("/api/stats").status_code == 402  # nothing recorded

    ch = client.post("/api/admin/face-challenge", json={"csrf": csrf})
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    r = client.post("/api/admin/activate", json={
        "email": DEMO_EMAIL, "csrf": csrf,
        "face_challenge_id": ch["challenge_id"], "face_distance": 0.3,
        "face_liveness": ch["action"],
    })
    assert r.status_code == 200, r.text
    assert auth.get("/api/stats").status_code == 200
    # The proof is single-use: replaying it fails.
    r = client.post("/api/admin/activate", json={
        "email": DEMO_EMAIL, "csrf": csrf,
        "face_challenge_id": ch["challenge_id"], "face_distance": 0.3,
        "face_liveness": ch["action"],
    })
    assert r.status_code == 403


def test_admin_challenge_needs_enrollment_first(client):
    _control_login(client)
    csrf = _admin_csrf(client)
    r = client.post("/api/admin/face-challenge", json={"csrf": csrf})
    assert r.status_code == 400
    assert "Enroll your face first" in r.json()["detail"]


def test_secret_header_activation_is_break_glass(auth):
    """The X-Admin-Secret API is the documented break-glass: it works
    with no face enrolled at all. No UI links to it — Control and the
    cookie API both demand the face proof (tests above)."""
    _make_unpaid()
    assert _templates() == []
    admin_activate(auth)
    assert auth.get("/api/stats").status_code == 200


def test_panel_pay_button_is_the_only_unlock_path(client):
    page = client.get("/").text
    # The Pay button (face-scan trigger) is in the panel markup…
    assert 'id="pay-face-btn"' in page
    # …and it leads to the DEDICATED scan page (owner's flow order):
    # QR page -> Pay -> "Scan Farhan's Face" page -> unlock. The scan
    # is a separate SPA view, never a section inside the panel.
    assert 'id="facescan-view"' in page
    assert "Scan Farhan's Face" in page
    assert 'id="scan-face-video"' in page
    assert 'id="pay-scan-area"' not in page
    # …the unlock completes ONLY through the face-verify call…
    assert "/api/face-unlock/verify" in page
    # …and there is no passive/poll unlock machinery left at all.
    assert "checkPaymentDone" not in page
    assert "startPaymentWatch" not in page
    assert "PAY_POLL" not in page
    assert 'id="face-unlock-slot"' not in page
