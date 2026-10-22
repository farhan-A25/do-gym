"""End-to-end API tests: auth, stats, check-in, renewal, add-ons, search, mode."""
from conftest import DEMO


# ---------------------------------------------------------------- auth ----
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_login_ok(client):
    r = client.post("/api/login", json=DEMO)
    assert r.status_code == 200
    assert r.json()["email"] == DEMO["email"]
    assert "phg_session" in r.headers.get("set-cookie", "")


def test_login_wrong_password_rejected(client):
    r = client.post("/api/login", json={"email": DEMO["email"], "password": "wrong-pass"})
    assert r.status_code == 401
    assert "Wrong email or password" in r.json()["detail"]


def test_login_unknown_email_rejected(client):
    r = client.post("/api/login", json={"email": "nobody@nowhere.in", "password": "x"})
    assert r.status_code == 401


def test_locked_routes_return_401_when_logged_out(client):
    for path in ["/api/me", "/api/stats", "/api/members", "/api/red-list",
                 "/api/renewals", "/api/addons", "/api/mode", "/api/search?q=a",
                 "/api/checkins/today"]:
        r = client.get(path)
        assert r.status_code == 401, f"{path} returned {r.status_code}"


def test_logout_ends_session(auth):
    r = auth.post("/api/logout")
    assert r.status_code == 200
    r = auth.get("/api/stats")
    assert r.status_code == 401


# --------------------------------------------------------------- stats ----
def test_dashboard_stats_on_seed(auth):
    r = auth.get("/api/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["total_members"] == 48
    assert s["red_list_count"] == 32
    assert s["checked_in_today"] == 5
    assert s["renewals_due"] == 6
    assert s["expired_count"] == 3
    assert s["addon_revenue_month"] == 8499
    assert s["renewed_this_month"] == 2000
    assert s["mode"] == "auto"


def test_red_list_top6_matches_video(auth):
    r = auth.get("/api/red-list")
    assert r.status_code == 200
    members = r.json()["members"]
    assert len(members) == 32
    top6 = [(m["name"], m["days_absent"]) for m in members[:6]]
    assert top6 == [
        ("Rohit A.", 16),
        ("Sandeep K.", 15),
        ("Prakash M.", 14),
        ("Amit S.", 13),
        ("Vikram G.", 12),
        ("Deepak P.", 11),
    ]


# ------------------------------------------------------------ check-in ----
def test_checkin_flow_and_duplicate_rejected(auth):
    # Rohit A. (PHG-001) is top of the Red List at 16 days absent.
    r = auth.post("/api/checkin", json={"code": "PHG-001"})
    assert r.status_code == 200, r.text
    assert r.json()["red_list_count"] == 31

    s = auth.get("/api/stats").json()
    assert s["red_list_count"] == 31
    assert s["checked_in_today"] == 6

    names = [m["name"] for m in auth.get("/api/red-list").json()["members"]]
    assert "Rohit A." not in names

    today = auth.get("/api/checkins/today").json()["checkins"]
    assert any(c["name"] == "Rohit A." for c in today)

    # Duplicate same-day check-in must be rejected politely.
    r = auth.post("/api/checkin", json={"code": "PHG-001"})
    assert r.status_code == 400
    assert "already checked in" in r.json()["detail"]


def test_checkin_unknown_code(auth):
    r = auth.post("/api/checkin", json={"code": "PHG-999"})
    assert r.status_code == 404


def test_checkin_by_member_id(auth):
    members = auth.get("/api/members").json()["members"]
    target = next(m for m in members if m["name"] == "Sandeep K.")
    r = auth.post("/api/checkin", json={"member_id": target["id"]})
    assert r.status_code == 200
    assert auth.get("/api/stats").json()["red_list_count"] == 31


# ------------------------------------------------------------- renewal ----
def test_renewal_flow_for_expired_member(auth):
    data = auth.get("/api/renewals").json()
    assert data["stats"]["expired_count"] == 3
    assert data["stats"]["due_count"] == 6
    expired = data["expired"][0]
    before_renewed = data["stats"]["renewed_this_month"]

    r = auth.post(f"/api/renewals/{expired['id']}/renew", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["member"]["days_left"] > 7  # expiry pushed well into the future

    data2 = auth.get("/api/renewals").json()
    assert data2["stats"]["expired_count"] == 2
    assert data2["stats"]["renewed_this_month"] == before_renewed + expired["amount"]
    assert any(h["member_name"] == expired["name"] for h in data2["history"])
    assert auth.get("/api/stats").json()["expired_count"] == 2


def test_renewal_custom_days_manual(auth):
    data = auth.get("/api/renewals").json()
    due = data["due"][0]
    r = auth.post(f"/api/renewals/{due['id']}/renew", json={"custom_days": 45})
    assert r.status_code == 200
    assert r.json()["member"]["days_left"] == 45


def test_renew_unknown_member(auth):
    r = auth.post("/api/renewals/99999/renew", json={})
    assert r.status_code == 404


# ------------------------------------------------------------- add-ons ----
def test_addon_assign_remove_and_revenue(auth):
    data = auth.get("/api/addons").json()
    assert data["revenue_month"] == 8499
    pt = next(a for a in data["catalog"] if a["id"] == "pt")
    assert pt["member_count"] == 1

    members = auth.get("/api/members").json()["members"]
    target = next(m for m in members if m["name"] == "Zara F.")

    r = auth.post("/api/addons/assign", json={"member_id": target["id"], "addon_id": "pt"})
    assert r.status_code == 200, r.text

    data2 = auth.get("/api/addons").json()
    assert data2["revenue_month"] == 8499 + 5000
    pt2 = next(a for a in data2["catalog"] if a["id"] == "pt")
    assert pt2["member_count"] == 2

    # The member profile now shows the add-on (single source of truth).
    prof = next(m for m in auth.get("/api/members").json()["members"] if m["id"] == target["id"])
    assert any(a["addon_id"] == "pt" for a in prof["addons"])

    # Duplicate assignment rejected politely.
    r = auth.post("/api/addons/assign", json={"member_id": target["id"], "addon_id": "pt"})
    assert r.status_code == 400

    sub = next(s for s in data2["active"] if s["member_id"] == target["id"] and s["addon_id"] == "pt")
    r = auth.delete(f"/api/addons/subs/{sub['sub_id']}")
    assert r.status_code == 200
    assert auth.get("/api/addons").json()["revenue_month"] == 8499


def test_remove_nonexistent_addon_sub(auth):
    r = auth.delete("/api/addons/subs/99999")
    assert r.status_code == 404


# --------------------------------------------------------------- search ----
def test_search_by_name(auth):
    r = auth.get("/api/search", params={"q": "Rohit"})
    assert r.status_code == 200
    hits = r.json()["members"]
    assert len(hits) == 1
    assert hits[0]["name"] == "Rohit A."


def test_search_by_code_case_insensitive(auth):
    r = auth.get("/api/search", params={"q": "phg-001"})
    hits = r.json()["members"]
    assert len(hits) == 1
    assert hits[0]["name"] == "Rohit A."


def test_search_no_match(auth):
    r = auth.get("/api/search", params={"q": "zzzzzz"})
    assert r.json()["members"] == []


# ----------------------------------------------------------------- mode ----
def test_mode_toggle_persists(auth):
    assert auth.get("/api/mode").json()["mode"] == "auto"
    r = auth.put("/api/mode", json={"mode": "manual"})
    assert r.status_code == 200
    assert auth.get("/api/mode").json()["mode"] == "manual"
    assert auth.get("/api/stats").json()["mode"] == "manual"
    r = auth.put("/api/mode", json={"mode": "auto"})
    assert r.status_code == 200
    assert auth.get("/api/mode").json()["mode"] == "auto"


def test_manual_flag_respected_by_red_list(auth):
    auth.put("/api/mode", json={"mode": "manual"})
    members = auth.get("/api/members").json()["members"]
    active = next(m for m in members if m["name"] == "Aryan K.")  # not on Red List
    r = auth.post("/api/red-list/flag", json={"member_id": active["id"]})
    assert r.status_code == 200
    red = auth.get("/api/red-list").json()["members"]
    assert len(red) == 33
    flagged = next(m for m in red if m["name"] == "Aryan K.")
    assert flagged["manual"] is True
    # Removing the flag restores the computed list.
    r = auth.delete(f"/api/red-list/flag/{active['id']}")
    assert r.status_code == 200
    assert len(auth.get("/api/red-list").json()["members"]) == 32


def test_pages_load(client):
    for path in ["/", "/privacy", "/terms"]:
        r = client.get(path)
        assert r.status_code == 200, path
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert "rest day" in r.text  # custom premium 404 page


# ------------------------------------------------- subscription plans ----
def test_plans_endpoint_exact_two_plans(client):
    # Public: the pricing page renders from this without any login.
    r = client.get("/api/plans")
    assert r.status_code == 200
    plans = r.json()["plans"]
    assert len(plans) == 2
    starter, pro = plans[0], plans[1]
    assert starter["id"] == "starter" and starter["monthly"] == 1299 and starter["yearly"] == 12990
    assert starter["member_limit"] == 150
    assert pro["id"] == "pro" and pro["monthly"] == 1899 and pro["yearly"] == 18990
    assert pro["member_limit"] is None
    assert pro["badge"] == "Most Popular"


def test_pricing_page_loads_without_login(client):
    r = client.get("/pricing")
    assert r.status_code == 200
    assert "pricing" in r.text.lower()


def test_demo_owner_starts_on_pro_monthly(auth):
    state = auth.get("/api/plan").json()
    assert state["plan_id"] == "pro"
    assert state["period"] == "monthly"
    assert state["status"] == "active (demo billing)"
    assert state["usage"]["members_used"] == 48
    assert state["usage"]["member_limit"] is None


def test_plan_switch_persists_after_client_reinit(auth):
    r = auth.post("/api/plan/switch", json={"plan_id": "starter", "period": "yearly"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == "starter"
    assert r.json()["usage"]["member_limit"] == 150

    # Simulate an app restart: a brand-new test client against the same app/DB.
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", json=DEMO)
    assert r.status_code == 200
    state = fresh.get("/api/plan").json()
    assert state["plan_id"] == "starter"
    assert state["period"] == "yearly"


# -------------------------------------------------------- entry flow ----
SIGNUP = {
    "owner_name": "Test Owner",
    "gym_name": "Test Gym",
    "email": "newgym@test.in",
    "mobile": "9876543210",
    "password": "secret1",
}
DETAILS = {
    "gym_name": "Test Gym",
    "gym_address": "Road No. 1, Main Market",
    "gym_city": "Hyderabad",
    "gym_phone": "9876543210",
    "opening_year": 2020,
    "owner_name": "Test Owner",
    "mobile": "9876543210",
    "whatsapp": "",
}


def test_full_signup_flow(client):
    # 1) Signup creates the account and logs the owner in.
    r = client.post("/api/signup", json=SIGNUP)
    assert r.status_code == 200, r.text
    me = client.get("/api/me").json()
    assert me["details_completed"] is False
    assert me["plan_chosen"] is False

    # 2) Details save — first a bad mobile is rejected with a field message.
    bad = dict(DETAILS, mobile="123")
    r = client.put("/api/details", json=bad)
    assert r.status_code == 400
    assert "mobile" in r.json()["fields"]
    # …then the correct details save and persist.
    r = client.put("/api/details", json=DETAILS)
    assert r.status_code == 200, r.text
    saved = client.get("/api/details").json()
    assert saved["gym_city"] == "Hyderabad"
    assert saved["gym_address"] == "Road No. 1, Main Market"
    assert client.get("/api/me").json()["details_completed"] is True

    # 3) Plan choice persists and completes the flow.
    r = client.post("/api/plan/switch", json={"plan_id": "starter", "period": "yearly"})
    assert r.status_code == 200
    assert client.get("/api/me").json()["plan_chosen"] is True
    plan = client.get("/api/plan").json()
    assert plan["plan_id"] == "starter" and plan["period"] == "yearly"

    # 4) The dashboard stats load for the new owner.
    s = client.get("/api/stats")
    assert s.status_code == 200
    assert s.json()["total_members"] == 48


def test_signup_duplicate_email_polite(client):
    dup = dict(SIGNUP, email="dup@test.in")
    r = client.post("/api/signup", json=dup)
    assert r.status_code == 200
    client.post("/api/logout")
    r = client.post("/api/signup", json=dup)
    assert r.status_code == 400
    assert "already exists" in r.json()["fields"]["email"]


def test_signup_validation_fields(client):
    r = client.post("/api/signup", json={
        "owner_name": "", "gym_name": "", "email": "not-an-email",
        "mobile": "123", "password": "x",
    })
    assert r.status_code == 400
    fields = r.json()["fields"]
    for key in ["owner_name", "gym_name", "email", "mobile", "password"]:
        assert key in fields, key


def test_login_with_incomplete_details_flags_flow(client):
    r = client.post("/api/signup", json=dict(SIGNUP, email="partial@test.in"))
    assert r.status_code == 200
    client.post("/api/logout")
    r = client.post("/api/login", json={"email": "partial@test.in", "password": "secret1"})
    assert r.status_code == 200
    me = client.get("/api/me").json()
    # The frontend uses these flags to route to the Details page.
    assert me["details_completed"] is False
    assert me["plan_chosen"] is False


def test_demo_login_goes_straight_to_dashboard(auth):
    me = auth.get("/api/me").json()
    assert me["details_completed"] is True
    assert me["plan_chosen"] is True
    assert me["gym_name"] == "PowerHouse Gym"


# --------------------------------------- UPI QR subscription payments ----
def test_payment_qr_requires_login(client):
    # The payment panel only exists inside the logged-in app.
    r = client.get("/payment-qr.png")
    assert r.status_code == 401


def test_payment_qr_served_to_logged_in_owner(auth):
    r = auth.get("/payment-qr.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes


def test_confirm_payment_activates_premium_yearly(auth):
    # The client tries to smuggle a fake amount — the server must ignore it
    # and record the billing.py price for Premium yearly (₹18,990).
    r = auth.post("/api/subscription/confirm-payment",
                  json={"plan_id": "pro", "period": "yearly", "amount": 1})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["plan_id"] == "pro"
    assert state["period"] == "yearly"
    assert state["payment_status"] == "active"
    assert state["price"] == 18990
    lp = state["last_payment"]
    assert lp is not None
    assert lp["amount"] == 18990
    assert lp["method"] == "upi_qr"
    assert lp["status"] == "paid"
    assert lp["plan_name"] == "Premium"
    assert lp["period"] == "yearly"


def test_confirm_payment_persists_after_client_reinit(auth):
    r = auth.post("/api/subscription/confirm-payment",
                  json={"plan_id": "pro", "period": "yearly"})
    assert r.status_code == 200, r.text

    # Simulate an app restart: a brand-new test client, same app/DB.
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", json=DEMO)
    assert r.status_code == 200
    state = fresh.get("/api/plan").json()
    assert state["payment_status"] == "active"
    assert state["plan_id"] == "pro" and state["period"] == "yearly"
    assert state["last_payment"]["amount"] == 18990
    assert state["last_payment"]["method"] == "upi_qr"
    assert state["last_payment"]["status"] == "paid"


def test_pay_later_sets_payment_pending_then_confirm_clears(auth):
    r = auth.post("/api/subscription/pay-later",
                  json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["plan_id"] == "starter"
    assert state["period"] == "monthly"
    assert state["payment_status"] == "payment_pending"
    lp = state["last_payment"]
    assert lp["status"] == "pending"
    assert lp["amount"] == 1299  # Standard monthly — computed server-side
    assert lp["method"] == "upi_qr"
    # The app stays fully usable while payment is pending (demo policy).
    assert auth.get("/api/stats").status_code == 200

    # Paying later from Plan & Billing flips it to active.
    r = auth.post("/api/subscription/confirm-payment",
                  json={"plan_id": "starter", "period": "monthly"})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["payment_status"] == "active"
    assert state["last_payment"]["status"] == "paid"
    assert state["last_payment"]["amount"] == 1299


def test_payment_endpoints_require_login(client):
    for path in ["/api/subscription/confirm-payment", "/api/subscription/pay-later"]:
        r = client.post(path, json={"plan_id": "pro", "period": "monthly"})
        assert r.status_code == 401, f"{path} returned {r.status_code}"
