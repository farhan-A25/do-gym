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

## 10. Google sign-in — "Continue with Google" (2026-10-22)

Real OAuth 2.0 / OpenID Connect sign-in, standard library only (urllib) —
no new dependencies, `requirements.txt` unchanged.

- **New module `backend/google_auth.py`** — environment config read per
  call (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `APP_BASE_URL`,
  default `http://127.0.0.1:8000`), `google_auth_enabled()` (true only
  when both credentials are set), authorize-URL builder, code→token
  exchange and userinfo fetch against Google's endpoints.
- **`backend/security.py`** — signed OAuth `state` helpers using their own
  itsdangerous salt (`phg-google-state-v1`, 10-minute max age), separate
  from the session cookie.
- **Routes (`backend/main.py`)**:
  - `GET /api/auth/config` → `{"google_enabled": bool}` (public).
  - `GET /auth/google` — disabled → 302 to `/?auth_error=…` (friendly,
    controlled); enabled → 302 to `accounts.google.com` with client_id,
    `redirect_uri={APP_BASE_URL}/auth/google/callback`, scope
    `openid email profile` and a fresh signed state. Start is covered by
    the existing login rate limiter.
  - `GET /auth/google/callback` — verifies state, exchanges the code,
    fetches userinfo, **requires `email_verified`**. Existing email →
    logged in with the normal session cookie (shared `_apply_session_cookie`
    helper, same Secure-flag logic). New email → account created
    (owner name from the Google profile; password column filled with a
    random unknown hash — Google-only account), `details_completed=false /
    plan_chosen=false`, so the existing route guard takes the owner to the
    Gym & Owner Details page, exactly like a fresh signup. Cancelled
    consent, bad/expired state, unverified email and Google-side failures
    all end in the same friendly `/?auth_error=…` redirect — never a 500.
- **Frontend (`frontend/index.html`)** — white "Continue with Google"
  button (official multicolour G as inline SVG) + "or" divider above the
  email form on BOTH the login and signup cards, inside a `.google-auth`
  block that starts `hidden`. On boot the page fetches `/api/auth/config`
  and unhides the blocks only when `google_enabled` is true (fetch failure
  = stays hidden). A `?auth_error=` return is shown in the login error box
  and scrubbed from the URL. CSP unchanged (inline SVG, no new origins).
- **Config/docs** — `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` /
  `APP_BASE_URL` documented in `backend/.env.example`; README gained a
  "Google sign-in" section (Cloud Console steps + the exact redirect URI
  to register) and the variables joined the configuration table.
- **Tests** — new `tests/test_google_auth.py` (11 tests, Google HTTP
  mocked at the `main` module boundary): disabled config + friendly
  redirects for both routes; enabled config; consent redirect parameters
  (client id, redirect URI, scope, state); callback creating a new user
  (email lower-cased, name from profile, entry-flow flags false, exactly
  one row) and logging them in (`/api/me` 200); callback logging in the
  existing demo owner without duplicating the account; unverified email,
  bad state, Google failure and user-cancelled flows all → friendly
  redirect with no account created.
- **Result** — full suite **48 passed** (37 pre-existing + 11 new). Live
  smoke test on temp databases: no keys → `google_enabled:false`, button
  block rendered `hidden`, `/auth/google` 302 with the friendly message;
  dummy keys → `google_enabled:true`, `/auth/google` 302 to
  `accounts.google.com` with the correct redirect URI. The real
  `backend/gym.db` was never touched (mtime unchanged across all runs).

## 11. QR self check-in — fully automatic member scans + PHG member codes (2026-10-23)

Built to the owner's explicit order: a member scans the QR poster on the
gym wall with their OWN phone and the website updates by itself (member
name, entry time, day) — the owner does nothing manually. Owner-side
check-in by code remains as a backup.

- **New module `backend/member_codes.py`** — both pieces of state live in
  the settings table: (a) the member-code counter `member_code_seq`,
  MONOTONIC — it only ever increases (bootstrapped from the highest code
  in use), so a PHG number is never reissued after a deletion;
  `next_member_code()` skips any code already held; `ensure_member_codes()`
  backfills NULL/empty codes. (b) the gym's check-in token
  (`secrets.token_urlsafe(16)`): lazily generated on first use, created
  for the demo account at seed, compared with `secrets.compare_digest`.
- **Public member flow** — `GET /checkin/{token}` (no login): unknown
  token → themed member-facing 404 (`frontend/checkin-404.html`); valid
  token → `frontend/checkin.html` with the gym's name + token injected —
  mobile-first, same black/white/blue theme, gym icon. The member types
  their PHG code once; the page stores it in `localStorage` (keyed by
  token) and AUTO-SUBMITS on every later visit (scan → present), with a
  "Not you? Use a different code" escape. `POST /api/checkin/scan`
  {token, code}: idempotent per member per day (repeat scan returns the
  first entry time, no second row), writes CheckIn with source "qr",
  answers {member name, code, time, date, gym name}. Rate-limited
  30/min per IP+token; a code only matches THIS gym's members and bad
  codes get one generic friendly answer — no owner data beyond the gym
  name is exposed.
- **Model** — `CheckIn.source` ("owner" | "qr", default "owner"); a
  startup migration adds the column to pre-existing databases
  (verified against an old-format DB: rows preserved, source backfilled
  to "owner"). Owner `POST /api/checkin` now tags source "owner";
  `/api/checkins/today` exposes `source`, so the owner can see which
  entries arrived by scan. Dashboard stats / Red List / today's list
  needed no changes — they read the same table, verified by test.
- **Owner endpoints** — `GET /api/gym/checkin-link` → {url, token,
  gym_name} with url = {APP_BASE_URL}/checkin/{token} (APP_BASE_URL
  reused from google_auth); `POST /api/gym/checkin-link/reset`
  regenerates the token (old posters die immediately, audit-logged).
  The QR Check-In screen's poster QR now encodes the FULL check-in URL,
  shows the gym name + "Members: scan with your phone camera to check
  in", the link text with a Copy button, and a "New Link" button.
  Owner-side code box + Simulate Scan unchanged.
- **Member creation** — new owner-only `POST /api/members`: validates
  name/phone/plan (+ optional expiry, default = plan length) with the
  app's field-error style and ALWAYS assigns the next PHG code
  server-side (the schema has no code field; a client-sent code is
  ignored — tested). Every member's code was already visible in the
  member lists, search, quick-find card and the per-member QR modal
  (owner hands the number to the member from there) — unchanged.
  Seed now also sets `checkin_token` + `member_code_seq=48`.
- **Tests** — new `tests/test_qr_checkin.py` (19 tests): link auth +
  shape/stability; member page 200 + gym name for a valid token and a
  friendly 404 for a bad one; scan happy path (row written with member,
  date, time, source "qr"); owner views updated through the existing
  APIs (Red List 32→31, checked-in 5→6); same-day idempotency (one row,
  first time returned); lowercase code accepted; wrong code → friendly
  404 + no row; wrong token → 404; rate limit → 429 after 30 attempts;
  token reset retires the old token (page + scan) and the new one works;
  all 48 seed members hold unique `PHG-NNN` codes; create assigns
  PHG-049, PHG-050…; client-supplied code ignored; delete-then-create
  does NOT reuse the number (049 deleted → next is 050); backfill gives
  a code-less member the next number; a freshly created member can scan
  in immediately. **Full suite: 67 passed (48 pre-existing + 19 new).**
- **Live smoke test** (real uvicorn, temp DB): owner login →
  checkin-link returned the APP_BASE_URL-based URL; member page 200
  with gym name + form and no leftover placeholders; unauthenticated
  scan of PHG-001 recorded "Rohit A. checked in at 11:51 AM"; repeat
  scan answered "Already checked in at 11:51 AM"; bad-token page 404;
  owner's today list showed the entry with source "qr"; stats moved to
  6 checked-in / Red List 31. The real `backend/gym.db` was never
  touched (tests + smoke all ran on temp databases).

## 12. Per-member QR removed from owner UI (2026-10-23)

Owner order: members were confused by two QRs — the product now has exactly
ONE QR, the big gym entry poster on the Check-In screen (encodes the
`/checkin/{token}` URL). The per-member QR feature is removed from
`frontend/index.html`:

- the small QR icon button (`data-qr`) on each member row of the check-in
  member list is gone — rows now show the member's PHG code as plain text
  only (e.g. "PHG-014"), alongside the existing Check In / Mark Present
  controls;
- the `[data-qr]` click binding and the whole `openQRModal()` function
  (per-member QR modal with `#modal-qr`) are deleted; no references remain
  anywhere in the owner UI (`grep` for `openQRModal|data-qr|modal-qr`
  returns nothing).
- Untouched, exactly as built: the gym entry QR (`drawQR` on `#gym-qr`,
  shared `drawQR` helper + QRCode library), the scan check-in flow, PHG
  member codes, Google auth, plans and the UPI payment panel.
- Tests: no test asserted the per-member QR UI (suite is API-level), so no
  test changes were needed. **Full suite: 67 passed.** Inline JS passes
  `node --check`.

## 13. Add Member UI + joining flow end-to-end (2026-10-23)

Part 1 of the owner's spec completed on the owner side: when a member
joins, their PHG number comes from the SYSTEM automatically — and the
owner now has the screen to do it.

- **What existed:** `POST /api/members` (auto-assigns the next PHG code
  from the monotonic counter, ignores any client-sent code) — but the
  owner UI had **no Add Member form at all**; the endpoint was
  unreachable from the app.
- **Added — Add Member card** on the Check-In screen (`frontend/index.html`,
  below "Check in by member code"): a toggle opens a clean form — member
  name, mobile number (marked optional), plan select (Monthly /
  Quarterly / Yearly). **There is deliberately no code field** — the
  owner can never type or see a PHG input. On submit it POSTs to
  `/api/members`; on success a toast announces "✅ <name> added — their
  PHG number is PHG-0XX. Tell them this number.", the form clears and
  closes, any active search is reset, and the member list + "in today"
  count re-render in place (no reload), so the new member's row with the
  auto-given PHG number is visible immediately. Validation errors land
  on the fields via the existing `showFieldErrs("am", …)` pattern.
- **Backend tweak (one rule):** in `create_member`, the phone is now
  optional — empty is accepted; a number that IS given must still be a
  valid 10-digit mobile (the existing "Bad Phone" validation test still
  passes unchanged).
- **End-to-end proof** — new test `test_joining_flow_end_to_end`
  (`tests/test_qr_checkin.py`): creates a member sending **no code and
  no phone** → asserts the response carries the UI-contract fields with
  `code == "PHG-049"` (next sequential) → immediately scan-checks the
  brand-new member via `/api/checkin/scan` with the gym token → asserts
  the owner's `/api/checkins/today` shows the entry with `source "qr"`
  and the scan's time, and `/api/stats` shows checked-in-today +1 and
  total members 48 → 49.
- **Public page re-verified live:** real uvicorn on a temp DB —
  `GET /checkin/{token}` with **no login** returned 200 with the gym
  name and the "Your member code" PHG entry (a plain URL any phone
  scanner or browser opens); a member created without a phone got
  PHG-049 and an unauthenticated scan recorded the entry, which the
  owner's today list showed with source "qr". The served app page
  contains the Add Member form. Real `backend/gym.db` untouched.
- **Full suite: 68 passed** (67 previous + the new end-to-end test).
  Inline JS passes `node --check`. Prices, plans, theme and Google auth
  unchanged; nothing committed or pushed.

## 14. Pay-first gate + Standard price change (2026-10-23)

Owner order (Part 2): *"what is these pay later — after they pay then
only the website should open and work."* Plus a same-day price update:
**Standard ₹1,299 → ₹1,599/mo and ₹12,990 → ₹15,990/yr** (yearly still
10 × monthly = 2 months free). Premium unchanged (₹1,899/mo ·
₹18,990/yr). Prices live only in `backend/billing.py`; the payment
panel amounts and `/api/plans` follow automatically (server-computed),
verified in tests and live below.

- **Pay Later removed completely.** The "Pay Later" button is gone from
  the payment panel (only **I Have Paid ✓** and a *‹ Back to plans*
  button that merely closes the panel — no state is saved), the
  `POST /api/subscription/pay-later` route is deleted (404 for everyone),
  the amber *Payment pending* badge UI is removed from Plan & Billing,
  and `_record_subscription_payment` now writes paid records only. A
  legacy `payment_pending` state (old rows / flags in an existing DB)
  is treated as **UNPAID** — gated to the payment step.
- **Hard server-side gate** (`subscription_gate` middleware in
  `backend/main.py`, decided by `subscription_info()` — one source of
  truth also exposed on `GET /api/plan` as `subscription_active`,
  `subscription_status`, `subscription_expires_on`). An authenticated
  owner whose subscription is not ACTIVE can reach ONLY: the Details
  step (`/api/details`), the Plans + payment step (`/api/plans`,
  `/api/plan`, `/api/plan/switch`,
  `/api/subscription/confirm-payment`, `/payment-qr.png`), `/api/me`,
  `/api/auth/config`, login/signup/Google/logout, and static/legal
  pages. Every other owner API (dashboard stats, members, check-ins,
  Red List, renewals, add-ons, mode, search, check-in link, reset)
  answers **402 `{"code": "payment_required", "subscription_status":
  …}`**. Unauthenticated requests pass through to the usual 401s.
  Choosing a plan (`/api/plan/switch`) no longer activates anything —
  it only records the choice; the gate lifts **only** when a payment is
  recorded. Frontend: `routeAfterAuth` checks `subscription_active`
  before entering the app, and any mid-session 402 routes the owner to
  the payment step with a clear note (unpaid / pending / expired copy).
- **ACTIVE definition / expiry.** The latest subscription payment is
  "paid" and today is inside its window — monthly = 30 days, yearly =
  365 days from the payment date (the app's existing period model; no
  trial, no grace). A lapsed plan re-gates to the same payment step
  with renewal copy ("Your plan has expired… Renew below"), and paying
  again re-opens the app at once. Plan & Billing shows an amber
  *Plan expired* badge with a **Renew Now** button for lapsed plans.
- **Demo account can never be locked out.** `seed_database()` now
  writes a real PAID `SubscriptionPayment` for the demo gym (Premium
  monthly, ₹1,899, `upi_qr`) plus `payment_status = "active"` settings,
  so the demo login is ACTIVE by the same rule as any payer.
- **Members are never gated.** `GET /checkin/{token}` and
  `POST /api/checkin/scan` stay public and work against a gated gym
  (proven live below while the owner got 402s).
- **Tests — full suite: 78 passed** (68 before; +10 new in
  `tests/test_payment_gate.py`, pay-later test replaced). New coverage:
  unpaid owner refused on every app API (direct calls, reads + writes);
  only the payment step stays reachable while unpaid; plan-switch alone
  does not unlock; legacy `payment_pending` = unpaid and paying clears
  it; paying lifts the gate immediately (Standard monthly recorded at
  the NEW ₹1,599; Standard yearly at ₹15,990); expired monthly plan
  re-gates and renewal re-opens; yearly plan still active at day 40;
  demo account active from its seeded payment; public scan works for a
  gated gym. Updated: plan-price assertions (₹1,599/₹15,990) and the
  payment-endpoint login test (pay-later now 404).
- **Live proof** — real uvicorn on a throwaway DB: demo login → stats
  200; `/api/plans` serves Standard 1599/15990, Premium 1899/18990;
  after wiping payments in the DB, the owner's direct `GET /api/stats`
  and `GET /api/members` return 402 `payment_required` while
  `/api/plan`, `/payment-qr.png` and `/` stay 200; a member scan
  (PHG-002) succeeds in that state; *I Have Paid* (Standard monthly)
  records ₹1,599 and stats immediately return 200; backdating the
  payment 40 days flips the status to `expired` and stats are 402
  again. The served app page contains no "Pay Later" anywhere. Real
  `backend/gym.db` data verified untouched (test/smoke DBs only;
  nothing committed or pushed).

## 15. QR-only panel, in-person collection + admin activation & Control (2026-10-23)

- **Panel decision (owner's order):** the payment panel shows ONLY his
  PhonePe QR + plan/amount + status line — no phone number, no extra
  payment options, NO checkout/pay button (the "Pay ₹X" idea was
  dropped by the owner). At this stage the only interactive element
  was Back. (The Pay button that later joined the panel is the
  face-scan trigger — §20.) Test:
  `test_served_panel_is_qr_plus_face_pay_button` — served HTML has no
  "I Have Paid", no "Pay Later", no checkout strings.
- **Honest limitation, stated plainly:** a static QR photo cannot
  tell the website a transfer happened. Automatic unlock therefore
  comes from a server-recorded confirmation — in the launch model,
  Farhan's own activation after collecting in person.
- **Collection model = PRIMARY activation:** Farhan visits each gym
  and collects in person. The owner self-confirm route
  (`POST /api/subscription/confirm-payment`) was REMOVED (404) — an
  owner can never self-activate.
- **Admin activation:** env `ADMIN_SECRET` (unset → admin surface is
  404). Secret-header API (`X-Admin-Secret`, constant-time compare):
  `GET /api/admin/gyms`, `POST /api/admin/activate` (records via the
  standard path, method `collected_in_person`, server-computed
  amount), `POST /api/admin/switch-off`. End-to-end test: owner gated
  (402) → admin activates → owner's `/api/plan` active → APIs 200.
- **Control page `/control`:** exists only with `ADMIN_SECRET`; login
  = the secret as password (rate-limited; wrong → 401); signed admin
  cookie (12h, HTTP-only, path-scoped to /control + /api/admin);
  form-token CSRF on POSTs; dashboard shows totals (gyms / active /
  ₹ collected) and one card per gym — owner name, gym, email,
  **Google ✓ verified vs Email** tag (from `User.auth_provider`),
  mobile, plan+period, status, expiry, joined date. No link to
  /control anywhere in the owner UI.
- **Switch Off:** per-account override (`revoked_user_<id>`) —
  `subscription_info()` treats the gym as not active at once; NOTHING
  is deleted. Documented decision: Activate on a switched-off gym
  clears the override and records a fresh payment ONLY if the plan/
  period differs from the live one or the window lapsed. Owner sees
  "Your plan is currently off. Please contact Do Gym to restart."
  Tests: activate → works → switch off → 402 everywhere + data
  present → activate → works, data intact.
- **Razorpay (dormant):** env-gated order endpoint + webhook
  (HMAC-SHA256 verified, constant-time; idempotent per payment id;
  order notes carry user_id). Tests fully offline: valid
  self-computed signature records + lifts the gate; bad signature
  records nothing; duplicate delivery records once; gateway-disabled
  order endpoint 404s. Launch steps in README (~2% fee stated).

## 16. Face unlock (2026-10-23)

- face-api.js 0.22.2 + weights (~7.5MB) vendored at
  `backend/static/face/`, served via a strict filename whitelist —
  NO runtime CDN. Only numeric templates (128-float descriptors ×
  poses) are stored (`FaceTemplate`); never photos. Enrollment lives
  in /control (3 poses: straight / slight left / slight right);
  re-register replaces.
- Owner scan: server issues a single-use challenge (120s TTL) with a
  server-chosen liveness action (blink / turn_left / turn_right);
  the browser matches locally (threshold 0.5) + checks the liveness;
  `POST /api/face-unlock/verify` burns the challenge on ANY attempt;
  success records method `face_unlock` via the standard path.
  5 failed verifies → 15-minute lockout. Switch Off overrides a
  face-unlocked gym; already-active accounts return `already_active`
  without recording.
- Tests (camera stubbed at the server seam): enrollment storage +
  replacement, template endpoint login-only, challenge issue/expiry/
  single-use, activation records + lifts gate, wrong liveness and
  over-threshold distance record nothing, lockout after 5 fails,
  switched-off gym refused, no-template → not available.
- **Honest limitation (owner's informed choice; he declined the
  phone-passkey route):** browser-side match + liveness is
  convenience-grade — photo spoofing is reduced, not eliminated; not
  bank-grade.

## 17. OTP + canonical signup flow + animated unlock (2026-10-23)

- Canonical order enforced server-side: **(1) Google/email account →
  (2) OTP → (3) Gym & Owner Details → (4) payment screen → (5)
  animated unlock → (6) app.** New accounts (incl. Google-created)
  start `otp_pending`; returning verified Google users skip OTP.
- OTP: 6 digits, stored as sha256(user:email:code), 10-minute TTL,
  max 5 attempts (then locked until resend), 60s resend cooldown.
  While pending, everything except the OTP endpoints + me/logout/
  config answers 403 `otp_required` (checked BEFORE the payment
  gate). Delivery: SMTP env-gated; `DEV_OTP_ECHO=1` (dev/tests only,
  default OFF) echoes the code in responses + stores a dev copy. The
  OTP code is logged ONLY under DEV_OTP_ECHO.
- Animated unlock: green card beat → full-screen overlay (SVG ring +
  check stroke-draw with glow, gym name) → wipe into the dashboard
  (~1.5s, tap skips). Used by the face-unlock path.
- Tests (24 in the face+OTP modules at the time): order enforced,
  wrong×5 locks, expired refused, resend cooldown, Google-new lands
  in otp_pending, full flow to active.

## 18. Add-Ons removed (2026-10-23)

- Owner's order. Backend endpoints deleted → 404; `member_dict` and
  stats no longer carry add-on data; frontend screen, nav tab (owner
  nav: Home · No-Show · Check-In · Renewal · Plan), home shortcut,
  member chips and all render code removed — zero "addon" references
  remain in the frontend or main.py. DB tables left in place
  harmlessly (no destructive migration). Test:
  `test_addons_section_is_gone`.

## 19. Security hardening pass (2026-10-23)

Plain-language summary: `SECURITY.md`. Attack tests:
`tests/test_security.py` (17 tests). Highlights:

- **BIG FINDING, FIXED — cross-account paywall bypass:** subscription
  state had an install-wide fallback, so a never-paid second account
  rode on another account's payment. State is now strictly
  per-account; /api/plan's recent payments are filtered to the
  account's own rows. Documented model: one install = one gym;
  accounts are that gym's team and share the member register, but
  payments/OTP/face/details/admin are strictly per-account and
  IDOR-tested (account B sees none of A's payments, cannot spend A's
  face challenge, cannot pass OTP with A's code).
- **Sessions:** tokens were deterministic (same-second logins could
  mint identical tokens) → random `jti` added. Logout now invalidates
  ALL of the account's tokens (`users.session_epoch` bump).
- **CSRF:** origin-check middleware on state-changing /api/* (403 on
  foreign Origin; webhooks + admin API exempt); Control forms keep
  their token CSRF (forged-token test proves nothing changes).
- **Headers:** full set on all responses incl. Permissions-Policy
  (camera=(self) only) and HSTS only under HTTPS. bcrypt cost 12
  verified. Logs mask emails/phones; generic 500 handler (no stack
  traces to clients); /health minimal; DB/backup/log/.env paths 404
  over HTTP; no string-built SQL anywhere.
- Env scaffolding: `.env.example` + `render.yaml` gained the SMTP
  block (DEV_OTP_ECHO deliberately NOT in render.yaml).

## 20. Face-gated unlock everywhere + plan limit (2026-10-23)

- **Owner correction (supersedes poll-only unlock):** the payment
  panel must NOT unlock by itself. The served owner UI contains NO
  polling machinery at all (`startPaymentWatch` / `checkPaymentDone`
  / `PAY_POLL` deleted — asserted by test). The ONE owner-screen
  unlock path: the panel's **Pay** button → face-scan session →
  verified → activation recorded → green "Payment Done" → animated
  unlock. Without a successful face verification the gym stays
  locked — no timers, no passive completion.
- **Same rule in Control:** the Activate button (page form AND the
  cookie-authenticated admin API) must carry a passed face
  verification (admin challenge: single-use, 120s, server-chosen
  liveness, distance ≤ 0.5) or NOTHING is recorded — tested: a plain
  form post leaves the DB byte-identical and the gym locked. The
  **secret-header API is the documented break-glass exception**
  (works with no face enrolled; no UI links to it; README +
  SECURITY.md say so plainly).
- **Not enrolled yet:** the Pay button explains "Face unlock is not
  set up yet — ask Do Gym to complete setup."; Control's challenge
  endpoint refuses with "Enroll your face first…".
- **Standard plan member limit 150 → 69** (`backend/billing.py`,
  single source — plans API + Plan & Billing usage follow
  automatically; assertions updated). Premium unchanged (unlimited).

## 21. Full end-to-end pass over the whole workflow (2026-10-23)

- Live uvicorn on a throwaway DB, scripted curl journey, **37/37
  checks passed**: signup → otp_pending → details 403 before OTP →
  wrong OTP refused, right OTP opens → details → plan → stats 402 →
  /control wrong password 401 → gym listed "Not Paid" → **page
  Activate without face proof stays locked** → with face proof
  activates (method `collected_in_person`, ₹1,599 from billing.py) →
  stats 200 → wall-QR scan (PHG-001, Rohit A.) → red list −1 → face
  enroll + a fresh gym unlocks by face (`face_unlock`) → backdated
  40 days → expired → reactivate → Switch Off → locked → restore →
  48 members + check-ins intact → self-confirm 404 → add-ons 404 →
  face assets 200 → Permissions-Policy present → second boot WITHOUT
  ADMIN_SECRET: /control + admin API 404.
- Full pytest suite after ALL changes above (incl. §§20–22):
  **145 passed, 0 failed**. Real `backend/gym.db` sha256 re-verified
  identical to the 2026-10-23 baseline
  (85c9fa42…b6824a5) — untouched. Nothing committed or pushed.

## 22. Dedicated face-scan page (2026-10-23)

- **Owner flow order (Farhan's refinement):** QR payment page (with
  the Pay button) → **Pay** → the dedicated full-screen **"Scan
  Farhan's Face"** page (its own SPA view: camera, status, liveness
  instructions, Cancel) → verified → the page turns green
  ("Payment Done ✓") → unlock animation → dashboard. Cancel or
  failure returns to the QR page, still locked. The scan is no
  longer a section inside the panel — the panel carries no video
  element at all (asserted: `pay-scan-area` absent, `facescan-view`
  + `scan-face-video` present in the served page).
- **Control:** Activate opens the same kind of dedicated page
  ("Scan Your Face", naming the gym) in place of the dashboard;
  a passed scan submits the activation; Cancel returns to the
  dashboard with nothing recorded. The in-card scan box is gone
  (asserted in tests).
- Backend challenge/verify endpoints unchanged.

## 23. Forgot Password — reset by emailed code (2026-10-23)

- **Why:** Farhan's own live launch testing got his test account
  stuck — there was no way back into an account whose password was
  forgotten. Now there is, built entirely from the existing OTP/auth
  machinery (same SMTP path, same code rules, same session-epoch
  mechanism as logout).
- **Endpoints:** `POST /api/auth/forgot {email}` and
  `POST /api/auth/reset {email, code, new_password}`. Both sit in
  GATE_OPEN_PATHS — password recovery is never paywalled and never
  OTP-gated.
- **No account enumeration (asserted in tests):** forgot answers one
  generic success body for known AND unknown emails, in all modes
  (the response never carries the code, not even under
  DEV_OTP_ECHO); an unknown email creates no challenge row and
  sends nothing. Per-IP and per-email send throttles (existing
  login limiter, prefixed buckets) plus the 60-second resend
  cooldown throttle the sending only — never the answer; a resend
  inside the cooldown keeps the first code valid (asserted).
- **Code rules = signup OTP rules (asserted):** 6 digits, stored
  SHA-256-hashed (64-hex, never the plain code), 10-minute TTL,
  5 attempts counted per wrong try, expired/spent code refused with
  one generic `reset_invalid` error, code consumed on success
  (replay refused). A too-short new password is rejected with the
  signup field error BEFORE any attempt is burned (asserted).
- **Purpose separation (asserted both directions):** reset codes
  live in a new `password_reset_challenges` table and are hashed
  with a `pwreset:` domain prefix — spending a signup OTP at
  /api/auth/reset fails, spending a reset code at
  /api/auth/otp/verify fails, and the reset hasher fed the signup
  code cannot reproduce the signup hash.
- **Session invalidation (asserted):** a successful reset replaces
  the bcrypt hash and bumps `session_epoch` — a cookie issued
  before the reset gets 401 from /api/me; old password login fails,
  new password login succeeds.
- **Not a verification (asserted):** resetting the password of an
  OTP-pending account leaves `otp_pending` true — next login with
  the new password still lands on the signup-OTP stage.
- **Delivery:** `_deliver_otp` was refactored onto a shared
  `_deliver_code` (one SMTP path, unchanged log lines/subject for
  the signup OTP); reset mail goes out as "Reset your Do Gym
  password" through the same path and the same no-SMTP rules (code
  never logged unless DEV_OTP_ECHO=1).
- **Frontend (served-page structure mirrors the OTP screen):**
  "Forgot password?" link on the login card → `forgot-view` with
  step 1 (email + Send reset code) and step 2 (6-digit code + new
  password with the .pw-eye show/hide toggle + Set new password),
  a resend link with the same 60s countdown display as the OTP
  screen, back-to-login on both steps, and success returning to
  login with the toast "Password changed ✓ — log in with your new
  password."
- **Tests:** tests/test_password_reset.py (10 tests) covers the
  full flow, unknown-email indistinguishability, cooldown
  behaviour, attempt counting/lockout, expiry, short-password
  rejection, both directions of purpose separation, otp_pending
  preservation, pre-reset session rejection, and reachability
  while logged in and unpaid.
