# VERIFICATION.md — PowerHouse Gym Owner App

Every check below was run for real on **2026-10-22** against the code in this folder.
Live checks ran against the actual server booted with a custom port (`PORT=8123`) to prove
deployment-style startup; automated checks are the pytest suite. Outputs are copied from the
real runs, not paraphrased.

---

## 1. Automated test suite (pytest)

```
$ .venv/bin/python -m pytest tests/ -q
31 passed, 1 warning in 25.17s
```

(The 1 warning is a harmless Starlette TestClient deprecation notice.)

Coverage: login ok/fail/unknown user, 401 sweep on 9 locked routes, logout → 401, seed stats
(48 / 32 / 5 / 6 / 3 / ₹8,499 / ₹2,000 / AUTO), Red List top-6 exactly matching the reference
video, check-in 32→31 + duplicate 400 + unknown code 404 + check-in by member id, renewal of an
expired member + custom-days extension + unknown member 404, add-on assign/remove with revenue
₹8,499 ↔ ₹13,499 + member profile shows the add-on + duplicate 400 + remove-missing 404,
search by name/code/case-insensitive/no-match, mode toggle persistence, manual flag respected
(32→33→32), pages `/`, `/privacy`, `/terms` = 200 + custom 404, **plans endpoint (exactly 2
plans, exact prices, "Most Popular" badge, Starter limit 150), pricing page 200 without login,
plan switch persists after a fresh client re-init**, and the **full entry flow** (signup →
details save persists → plan choice persists → dashboard stats load; bad mobile rejected with a
field message; duplicate signup email handled politely; login with incomplete details reports
`details_completed=false / plan_chosen=false` so the app routes to the Details page; demo login
reports both complete).

## 2. Deployment boot proof (custom port)

Server started from `backend/` with `PORT=8123 python main.py` (uvicorn on 0.0.0.0:8123):

```
GET http://127.0.0.1:8123/health  →  {"status":"ok","database":"ok"}
```

All live checks below ran on this port-8123 instance.

## 3. Live QA — Phase A (44/44 passed)

```
PASS | login page loads | HTTP 200
PASS | /privacy loads without login | HTTP 200
PASS | /terms loads without login | HTTP 200
PASS | /pricing loads without login | HTTP 200
PASS | health endpoint | {"status": "ok", "database": "ok"}
PASS | config demo hint ON locally | {"show_demo_hint": true}
PASS | plans public, exactly 2, exact prices | Starter 499/4990 · Pro 999/9990 · Pro badge "Most Popular"
PASS | security headers present | XCTO=nosniff XFO=DENY CSP=yes
PASS | unauthenticated API = 401 | HTTP 401
PASS | wrong password rejected politely | HTTP 401: Wrong email or password. Please try again.
PASS | empty login fields handled (no 500) | HTTP 422 (frontend also blocks empty submits with a friendly message)
PASS | demo login succeeds | HTTP 200
PASS | demo goes straight to dashboard (details+plan complete) | {"email": "owner@powerhousegym.in", "gym_name": "PowerHouse Gym", "details_completed": true, "plan_chosen": true}
PASS | first-load stats 48/32/5/6/3, addons 8499, renewed 2000, AUTO | {"total_members": 48, "checked_in_today": 5, "red_list_count": 32, "renewals_due": 6, "expired_count": 3, "addon_revenue_month": 8499, "renewed_this_month": 2000, "mode": "auto"}
PASS | red list = 32, video top-6 exact | [('Rohit A.', 16), ('Sandeep K.', 15), ('Prakash M.', 14), ('Amit S.', 13), ('Vikram G.', 12), ('Deepak P.', 11)]
PASS | search 'Rohit' = exactly Rohit A. | 1 result (PHG-001)
PASS | search by code, case-insensitive | 'phg-001' → Rohit A.
PASS | search no-match = empty list, no error
PASS | check-in Rohit A. by code | HTTP 200
PASS | red list 32 -> 31, checked-in 5 -> 6
PASS | Rohit in Today's Check-Ins
PASS | duplicate check-in = friendly 400 | "Rohit A. is already checked in today."
PASS | unknown code = friendly 404 | "Member not found. Check the code and try again."
PASS | renew expired member | Ira T. (PHG-046) renewed
PASS | expired 3 -> 2, renewed revenue up by plan amount, history has member | expired=2, renewed=3000 (was 2000, +1000 Monthly)
PASS | assign Personal Training | "Personal Training assigned to Sandeep K."
PASS | revenue +5000 and member profile shows the add-on | revenue 8499 -> 13499; Sandeep addons=['Personal Training']
PASS | duplicate add-on assign = friendly 400 | "Sandeep K. already has Personal Training."
PASS | remove non-existent add-on = friendly 404 | "Subscription not found."
PASS | mode toggle to MANUAL persists in stats
PASS | manual flag adds member to red list with MANUAL tag | count=32
PASS | manual flag removed again | count=31
PASS | demo starts PRO monthly | {"plan_id": "pro", "period": "monthly", "status": "active (demo billing)"}
PASS | switch to Starter yearly, usage limit 150
PASS | fresh signup creates account + session | HTTP 200
PASS | fresh signup: details + plan incomplete (flow required) | details_completed=false, plan_chosen=false
PASS | details with bad phone = field error, no 500 | HTTP 400 with fields.mobile message
PASS | details save persists
PASS | details read back correctly | gym_city=Hyderabad, email=liveqa@test.in
PASS | plan choice completes the flow | plan_chosen=true
PASS | new owner reaches dashboard stats | total_members=48
PASS | duplicate signup email = polite 400 | fields.email: "already exists"
PASS | pre-restart state set: Starter yearly
PASS | after logout every API route = 401 | /api/stats, /api/members, /api/red-list, /api/renewals, /api/addons, /api/mode, /api/plan, /api/me, /api/details, /api/checkins/today, /api/search — all 401
== PHASE A: 44/44 passed ==
```

The integration chain is proven by the same run: check-in moved the Red List **and** Home stats
**and** Today's Check-Ins; renewal moved Expired, revenue and history; add-on assign moved
revenue **and** the member profile; manual flags were respected by the computed list; search
behaviour on the Red List itself is frontend filtering over the same `/api/red-list` data this
run verified (see §6 note).

## 4. Restart persistence + idempotent seed (Phases B and C)

After Phase A, the server was **stopped and restarted**, then the checks ran again;
then it was **restarted a second time** and the identical checks ran again. Both restarts:

```
PASS | login after restart | HTTP 200
PASS | check-in persisted: red list still 31, checked-in 6
PASS | renewal persisted: expired still 2, renewed this month 3000
PASS | add-on revenue persisted: 13499
PASS | no duplicate seeding: still 48 members
PASS | plan switch persisted: Starter yearly
PASS | Sandeep still shows Personal Training
PASS | renewal history still contains Ira T.
== RESTART PHASE: ALL PASS ==   (identical output on restart #1 and restart #2)
```

## 5. Final tidy-up + error audit

- Live database reset to pristine demo state at the end of QA:
  `POST /api/reset` → stats `{"total_members": 48, "checked_in_today": 5, "red_list_count": 32, "renewals_due": 6, "expired_count": 3, "addon_revenue_month": 8499, "renewed_this_month": 2000, "mode": "auto"}` and plan back to **Pro (Monthly)**.
- Server logs (`logs/app.log` + uvicorn output for all three boots) grepped for 500s / tracebacks / exceptions: **zero**. (One incidental grep hit was the number "38500" in an access-log line — a client port, not an error.)
- Plan changes are audit-logged: `logs/app.log` contains 7 `Plan switched by … status: active (demo billing)` lines from the QA runs.
- Daily backup works: `backups/gym-2026-10-22.db` was created on startup (last 7 kept).

## 6. Code-level checks (no live browser available to this agent)

- **JavaScript syntax:** the app's inline script was extracted and checked with `node --check` → `JS SYNTAX OK` (after all entry-flow and Plan-screen code was added).
- **Mobile ~390px:** verified at code level — viewport meta tag, app column capped at 430px, 6-tab bottom nav with small labels, all tap targets ≥44px in CSS, no fixed-width content that can overflow. A pixel-level pass in a real device browser was not possible in this environment.
- **QR fallback:** the check-in screen uses the qrcodejs CDN, and the code was verified to fall back to a locally drawn canvas QR-look pattern **and** the member code printed as text (e.g. `PHG-001`) whenever the CDN is unreachable — check-in by typed code always works offline of the CDN.
- **Rate limiting observed working:** during QA, repeated login attempts from one IP correctly returned `429 "Too many login attempts. Please wait a minute and try again."` and cleared after the window — the limiter was then waited out and the remaining checks passed.
- **Frontend click-path:** every button/link in the UI is wired in code (nav tabs, filters, modals, renew, assign/remove, mode toggle, plan switching, settings edit, logout, legal links) and every action they call was exercised through its API in §3; session expiry mid-use is handled by the API layer (any 401 returns the user to the login page with the polite note "Your session has ended. Please log in again to continue.").

---

## 7. UPI QR subscription payments (added 2026-10-22)

What was added: a payment panel (Upgrade step + Plan & Billing) with the
owner's UPI QR and the exact plan amount; **I Have Paid**
(`POST /api/subscription/confirm-payment`) and **Pay Later**
(`POST /api/subscription/pay-later`); the QR served from
`GET /payment-qr.png` (login required) out of the single file
`backend/static/payment-qr.png` — the owner's **real PhonePe QR** (614×614,
installed by the owner; md5 verified identical between the file and the
served response). Amounts are computed server-side from `billing.py` only;
payments are stored in the new `subscription_payments` table and
audit-logged; Plan & Billing now shows the server-computed price, payment
status, an amber **Payment pending** badge + Pay Now button when pending,
and "Paid via UPI QR · ₹… · Active since …" + the last payment when paid.

Automated tests — the suite grew from 31 to **37 passed**:

```
$ .venv/bin/python -m pytest tests/ -q
37 passed, 1 warning in 30.95s
```

New tests: QR route 401 logged-out / 200 `image/png` with real PNG bytes
logged-in; confirm-payment records ₹18,990 for Premium yearly even when the
client sends `amount: 1`; confirm persists across a fresh client re-init;
pay-later sets `payment_pending` (₹1,299 Standard monthly) with the app
still usable, and a later confirm flips it to `active`; both payment
endpoints 401 when logged out. Frontend JS re-checked: `node --check` →
**JS SYNTAX OK**.

Live walk (server booted on port **8137** against a *copy* of the database,
so the real `gym.db` stayed pristine; demo owner login):

```
GET  /payment-qr.png            (no login)   -> 401
POST /api/subscription/confirm-payment (no login) -> 401
POST /api/login                              -> 200
GET  /payment-qr.png            (logged in)  -> 200 image/png
     md5 served == md5 backend/static/payment-qr.png (ff12d5bce072519c922fc1f38b96c2f1)
GET  /api/plan                               -> pro / monthly / price 1899 / payment_status null / last_payment null
POST /api/subscription/pay-later {starter, monthly}
                                             -> payment_status payment_pending,
                                                last_payment {Standard, monthly, 1299, upi_qr, pending, 2026-10-22}
POST /api/subscription/confirm-payment {pro, yearly, amount: 1}
                                             -> "Payment recorded — your Premium plan (yearly) is active. Thank you!"
                                                plan pro / yearly / price 18990 / payment_status active / since 2026-10-22
                                                last_payment {Premium, yearly, 18990, upi_qr, paid, 2026-10-22}
```

Restart persistence: server killed and rebooted against the same database
copy; after a fresh login `GET /api/plan` still returned
`pro / yearly / payment_status active / since 2026-10-22` with
`last_payment` = Premium yearly ₹18,990 paid via UPI QR and **2** payment
history rows. Audit log (`logs/app.log`) gained the matching lines, e.g.
`Subscription payment PAID by owner@powerhousegym.in -> pro (yearly), amount ₹18990 via UPI QR`
and the earlier `… PENDING (pay later) … -> starter (monthly), amount ₹1299 via UPI QR`.
The temp database copy was deleted afterwards; the live demo data is
unchanged (still pristine, no payment recorded).

## 8. Re-theme (black & white + blue, "Meta Muse" style) + product rebrand to "Do Gym" (2026-10-22)

Colour pass only — structure, layout, screens and functions unchanged. Applied
to `frontend/index.html`, `pricing.html`, `privacy.html`, `terms.html`,
`404.html`:

- Palette: page `#000000`, panels/cards `#0a0a0b` / `#101012` / `#17171b`,
  borders dark greys (`#232326` family / white-alpha), headings `#ffffff`,
  body `#d7d7db`, muted `#8e8e93`. Single accent blue `#0a84ff` with
  `#2ea8ff` / `#0060df` gradient ends (gradients only where one already
  existed); blue glows, focus rings, toggles, mode pill, badges, nav.
- The four gradient shortcut cards are now blue-family gradients
  (bright `#0a84ff→#2ea8ff` / deep `#0a84ff→#0060df`) with blue glows.
- Only red left in the UI is the pre-existing danger semantics: the No-Show
  RED LIST (numbers/badges/call buttons), error boxes and the Exit button.
  Amber "payment pending" and green "active" badges unchanged (status
  semantics, not theme). The payment QR card stays white (scannability).
- Token audit: every hex/rgba token in `frontend/*.html` enumerated — no
  pink/violet value remains (old identifiers `--pink`, `--violet`,
  `sc-pink`, `btn-violet`, … were renamed to blue names; values verified).

Product rebrand: the website/product is now **Do Gym** — login brand header
("Do Gym" with gradient-blue "Gym", existing tagline kept), `<title>` tags
("Do Gym", "Plans & Pricing — Do Gym", "Privacy Policy — Do Gym",
"Terms of Use — Do Gym", "Page Not Found — Do Gym"), privacy/terms body
references, README title/intro. NOT changed (demo data, by design): the
seeded demo gym's business name stays "PowerHouse Gym"
(`backend/seed.py`, header gym-name slot, sample-name input placeholders,
the demo gym's own check-in QR label) and the demo login email — the test
suite's `gym_name == "PowerHouse Gym"` assertions still pass.

Checks run:

```
$ .venv/bin/python -m pytest tests/ -q
37 passed, 1 warning in 32.11s

$ node --check (inline scripts extracted from index.html + pricing.html)
OK — both parse clean
```

Live screenshots (server on port 8137 against a throwaway temp DB,
chromium headless shell, 430×932 @2x, viewed by the agent):

- **Login** — true-black page, blue gradient logo mark + glow, "Do Gym"
  brand, near-black card, blue gradient Log In button, blue demo-hint box,
  grey legal links. No pink/violet. PASS.
- **Dashboard** — blue AUTO MODE pill + toggle, Premium Plan card, white
  stat numbers with the Red List count in red (32, intentional), blue
  SCREEN 1/2 shortcut cards, blue active nav icon. Header shows
  "PowerHouse Gym" — the demo gym's name from data, as designed. PASS.
- **Pricing** — blue back link + gradient headline, blue Monthly toggle,
  blue ✓ lists, blue CTA, blue MOST POPULAR badge + border on Premium
  (Standard ₹1,299 / Premium ₹1,899). PASS.

The real `backend/gym.db` was never touched (temp DATABASE_URL used for the
screenshot server; file unchanged, 69,632 bytes).

## 9. Final brand icon installed (2026-10-22)

The owner's final icon pick (chrome "DG" + blue lightning on a glossy black
tile) was installed:

- Assets created from the source artwork with PIL (artwork unmodified):
  `frontend/icon-512.png` (512×512), `frontend/icon-192.png` (192×192),
  `frontend/favicon-32.png` (32×32).
- FastAPI routes added (public, no session needed — browsers fetch favicons
  unauthenticated): `GET /icon-512.png`, `/icon-192.png`, `/favicon-32.png`.
- Favicon + apple-touch-icon <link> tags added to all 5 pages (index,
  pricing, privacy, terms, 404).
- Icon placed in the brand spots, replacing the CSS/SVG dumbbell mark with
  layout unchanged: login, signup and details brand headers (icon-512),
  the in-app header next to the gym name (38px, icon-192), and a new brand
  row at the top of the pricing page (40px icon + "Do Gym" wordmark).
  Rounded tile shape and the theme's blue glow shadow kept throughout.
- Verification: full test suite **37 passed**; server booted on port 8139 —
  all three icon URLs returned 200 `image/png` (232,125 / 44,170 / 2,128
  bytes); login page screenshot at 430×932 (chromium headless shell) viewed:
  the chrome DG icon sits cleanly in the brand header above the "Do Gym"
  wordmark, no distortion, no layout break. PASS.
- DB and backend logic untouched.
