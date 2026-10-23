# Do Gym

A complete gym-owner web app that stops members from silently quitting. The owner sees **who has stopped coming** (the No-Show Red List), checks members in by **QR code**, handles **renewals** before they lapse — all from one premium dashboard in a black & white + blue theme (Meta Muse style; the No-Show Red List keeps its red danger accents).

Built as a real full-stack website: **FastAPI + SQLite backend** with a secure owner login, and a mobile-style frontend served by the same app.

---

## The Screens

| Screen | What it does |
|---|---|
| **Home / Dashboard** | Live stats (Total Members, Checked In Today, Red List count, Renewals Due), member quick-find search with a status card (last visit, plan/expiry, Call / Check-In buttons), AUTO/MANUAL mode card, current-plan strip, and Today's Check-Ins. |
| **Screen 1 — No-Show Red List** | Members absent 10+ days, biggest absences first, with a red call button per member. Filter cycles 10+ / 15+ / 20+ days. In Manual mode the owner can flag or clear members by hand. |
| **Screen 2 — QR Check-In** | The gym's QR poster — it encodes the gym's public check-in link, so members scan it with their **own phone** and check in by themselves (see *QR self check-in* below); plus owner-side check-in by member code (e.g. `PHG-001`) and a Simulate Scan button. Duplicate check-ins on the same day are politely rejected. |
| **Screen 3 — Renewal** | Memberships expiring within 7 days and expired memberships, each with a Renew button (Manual mode: extend by a custom number of days), plus renewal history and "Renewed this month" revenue. |
| **Plan & Billing** | The gym's current subscription, member usage, the payment panel (his UPI QR + plan/amount + the **Pay** face-scan button — see *How owners actually pay* below), payment status and last payment, and Settings (Gym & Owner Details). |

**Entry flow for a new gym (the canonical order):** Continue with Google (or email signup) → **OTP** to that email → Gym & Owner Details → Choose a Plan → Payment screen → face-scan unlock **with an animation** → Dashboard. Two gates guard the app, enforced on the server: until the OTP is verified the account can reach only the OTP endpoints, and until the subscription payment is recorded every app API answers 402 — the owner can only finish the entry flow and pay (see *OTP verification* and *How owners actually pay* below).
The demo account already completed the flow, so it goes straight to the Dashboard.

**AUTO / MANUAL modes:** In AUTO (default), every list, count and revenue figure updates itself from the database. In MANUAL, automatic highlights pause and the owner flags/clears the Red List, marks attendance by tapping names, and extends renewals by custom days. Manual flags are stored separately and respected by the automatic logic in every section.

**Search everywhere:** Every section has a live search bar (name or member code, case-insensitive), with "Showing X" counts and a friendly "No member found" state.

Everything is connected through **one source of truth — the database**: a check-in removes the member from the Red List and updates Home instantly; a renewal updates counts, history and revenue everywhere; an add-on shows on the member's profile in every section.

---

## Subscription Plans (for using this website)

Plans are defined once in the backend (`backend/billing.py`) and served via `GET /api/plans`; the public `/pricing` page and the in-app Plan & Billing screen both render from it.

| Plan | Monthly | Yearly (2 months free) | Includes |
|---|---|---|---|
| **Standard** | ₹1,599/mo | ₹15,990/yr | Up to 69 members, Red List, QR Check-In, Renewals, search everywhere, AUTO mode, 1 owner login |
| **Premium** ⭐ Most Popular | ₹1,899/mo | ₹18,990/yr | Unlimited members, everything in Standard, AUTO + MANUAL modes, renewal history & reports, priority support |

*Prices in INR. GST extra as applicable. Cancel anytime.*
Market basis: Indian gym software competitors run ₹399–₹1,999/month (GymMunshi, Fitzenix, GymExec, FitnessDesk), and yearly billing across the market works out to about 2 months free.

**How owners actually pay (the launch model — pay first, the website works only after payment):** Farhan personally visits each gym and collects the subscription money in person. Choosing a plan (in the entry flow's Upgrade step, or later in Plan & Billing) opens the payment panel: the plan summary, **Farhan's PhonePe UPI QR** (`backend/static/payment-qr.png` — the owner can scan-and-pay it while Farhan is there, or pay cash/in person), the exact amount taken from `billing.py`, and exactly **one action — the Pay button**.

**One rule everywhere: no face, no unlock.** The panel NEVER unlocks by itself — no polling, no timers, no "I have paid" button (removed on the owner's order; an owner can never self-activate). Pressing **Pay** leaves the QR page for the dedicated **"Scan Farhan's Face" page**: Farhan — physically present after collecting — looks at the camera on the owner's device; the browser matches his face against the template he enrolled in Control, with a server-chosen liveness beat (blink / head turn). Only a **verified** scan records the activation (method `face_unlock`, server-computed amount) — the scan page turns green **"Payment Done ✓"**, the unlock animation plays, and the gym opens. Cancelling or failing the scan returns to the QR page, still locked. Full face details + the honest limitation: *Face unlock* below.

There is **no Pay Later** (removed 2026-10-23 on the owner's order). A server-side gate refuses every app API with `402 {"code": "payment_required"}` until a payment is active; the owner can reach only the Details step, the Plans + payment step, the face-unlock endpoints, `/api/me`, `/api/plans`, `/api/auth/config`, logout and the legal pages. Choosing a plan by itself does not unlock anything. When a paid period ends (30 days monthly / 365 days yearly from the payment date), the gate closes again and the payment step becomes a renewal; expiry can also be ended instantly by Farhan's Switch Off (see *Your control*). The public member QR check-in is never gated — members keep scanning even while the owner is locked out.

Amounts are **always computed server-side from `billing.py`** — any amount a browser sends is ignored — and every payment is written to the server audit log (`logs/app.log`). The demo gym starts on Premium (monthly) backed by a seeded **paid** payment record, so the demo login is always active; its seeded status reads "active (demo billing)" until the first recorded payment is made.

**Activating from Farhan's side (Control — the normal route):** after collecting, Farhan opens `/control` (below), taps **Activate** on the gym and passes the same face scan on **his own device** — the activation records with method `collected_in_person` and the owner's app unlocks. For scripts/emergencies there is a **break-glass API** (no UI links to it): `POST /api/admin/activate` with header `X-Admin-Secret: <ADMIN_SECRET>` and JSON `{"email": "owner@gym.in", "plan_id": "starter", "period": "monthly"}` (plan/period optional — the gym's chosen plan is used). `GET /api/admin/gyms` lists every gym with its status; `POST /api/admin/switch-off` applies Switch Off.

**Why a static QR can't unlock by itself:** a QR picture is just an image — when money lands in a UPI account, the picture learns nothing, so the website cannot detect the transfer on its own. That is exactly why the confirmation is Farhan's face scan (launch model). A fully automatic online route exists but stays **dormant** until you switch it on — see *Optional: automatic online payment (Razorpay)* below.

## Your control (Farhan's page) — `/control`

Farhan's own page for running the platform. It **exists only when `ADMIN_SECRET` is set** in the environment — without it, `/control` and the admin API answer 404, and no screen anywhere links to it.

- **Login** is a simple password form: the password IS `ADMIN_SECRET`. A correct login sets a separate, signed admin cookie (12 hours, HTTP-only, path-scoped to `/control` + `/api/admin`) — it is not an owner session. Wrong passwords are refused and rate-limited (5/minute), and every form on the page carries a secret CSRF token.
- **The dashboard** shows totals — gyms, how many are Active, and **total ₹ collected** (the sum of all recorded payments) — and one card per gym account: owner name, gym name, email, a **"Google ✓ verified"** or **"Email"** badge (how the account signed up), mobile, chosen plan + period, status with expiry, and the joined date. Statuses: **Active ✓ / Not Paid / Expired / Switched Off**.
- **Activate** (for a gym Farhan just collected from): records the payment (method `collected_in_person`, the gym's chosen plan/period, amount computed server-side) and the gym unlocks at once. Activate first runs **Farhan's face scan on his own device** — no face, no unlock, exactly like the owner's Pay button. Re-activating a switched-off gym inside its still-valid paid window simply clears the off-switch (no double charge in the totals).
- **Switch Off** (two-tap confirm): locks that gym instantly, even mid-paid-period — the owner's app falls back to the payment screen with *"Your plan is currently off. Please contact Do Gym to restart."* **Nothing is deleted**; members and check-ins stay exactly as they were, and Activate restores them.
- **Face enrollment** lives here too (the "Your face — gym unlock" card): 3 camera poses → only the numeric template is stored (see *Face unlock*).
- The same actions exist as a **scripting API** (`X-Admin-Secret` header): `GET /api/admin/gyms`, `POST /api/admin/activate`, `POST /api/admin/switch-off`. The header path is the documented **break-glass** (curl example in *How owners actually pay* above); the browser paths — page and cookie API — always demand the face proof.

---

## Face unlock

The unlock moment (owner's decision, 2026-10-23): Farhan is physically at the gym after collecting; the owner opens the site, taps **Pay** on the payment panel, and lands on the dedicated **"Scan Farhan's Face" page** — Farhan's face scan on that device is the confirmation. The same dedicated scan page guards Control's Activate on Farhan's own device ("Scan Your Face").

- **Enrollment (once):** in `/control`, "Register my face" runs the camera for 3 poses (straight / slight left / slight right). A small face-recognition library (`face-api.js` 0.22.2) is **vendored under `backend/static/face/`** and served by the app itself — no CDN at runtime. Only the **numeric face template** (128 numbers per pose) is stored in the database — **never a photo**. Re-registering replaces it; removing it hides face unlock everywhere.
- **The scan:** the server first issues a **single-use challenge (120 seconds)** that names a secret liveness action — **blink**, **turn left** or **turn right**. The browser matches the live face against the enrolled template on-device and checks the liveness movement; then it sends the challenge id + match distance + liveness result to `POST /api/face-unlock/verify`. The server accepts only a live, unused challenge with the right liveness and a distance ≤ 0.5, burns the challenge on any attempt, and only then records the payment (method `face_unlock`) and lifts the gate. **5 failed attempts lock face unlock for that gym for 15 minutes.** Switch Off overrides a face-unlocked gym until Farhan reactivates.
- **Honest limitation (the owner's informed choice — he declined the phone Face-ID/passkey route):** the match runs in the browser, so the server verifies the *claim*, not the camera. The challenge + liveness + rate limits make photo spoofing and casual cheating hard, but this is **convenience-grade, not bank-grade** — the real-world safety net is that money changes hands with Farhan present, plus his Switch Off.

---

## OTP verification (signup step 2)

Every new account — Google or email — passes an **email OTP** before Gym & Owner Details, in the owner's canonical order: **Google/email → OTP → details → payment → animated unlock → app**.

- A **6-digit** code, **stored hashed** (never in plain text), valid **10 minutes**, **max 5 attempts** (then a fresh code is needed), **resend after 60 seconds**, all rate-limited. While an account is unverified, the server answers everything except the OTP endpoints (+ `/api/me`, logout, config) with `403 {"code": "otp_required"}` — details, plan and app are unreachable.
- **Delivery is by email via SMTP** (env-gated: `SMTP_HOST/PORT/USER/PASS/FROM`). **Publish-day step:** use Gmail — set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` + `SMTP_FROM` to the mailbox, and `SMTP_PASS` to a **Gmail App Password** (Google Account → Security → App passwords). Until those are set, no email can leave the server: the app logs that a code was issued but never the code itself.
- **Testing without email:** set `DEV_OTP_ECHO=1` (local/dev only) and codes are echoed in the API response + log. It is **off by default and must stay off live**.

When the payment gate lifts (face unlock, Control activation, or the dormant webhook), the owner sees the **animated unlock**: the panel's card turns green ("Payment Done ✓"), then a full-screen animation — a ring draws itself, the check mark strokes in with a glow, the gym's name appears — and the screen wipes up into the dashboard (about 1.5 s; a tap skips it).

---

## Password reset ("Forgot password?")

The login screen carries a **Forgot password?** link. The owner enters their account email → the server emails a **6-digit reset code** (same SMTP path as the signup OTP) → they enter the code + a new password on the same screen.

- `POST /api/auth/forgot` **always** answers the same generic success — *"If an account with this email exists, a reset code has been sent."* — whether the account exists or not, and whether a rate limit/cooldown quietly swallowed the send. It can never be used to probe which emails have accounts. Sends are rate-limited per IP and per email, with the signup OTP's 60-second resend cooldown.
- `POST /api/auth/reset` checks the code under the signup OTP's rules (stored **hashed**, **10 minutes**, **max 5 attempts**, one generic failure answer for wrong/expired/spent codes). New password rule is signup's: **at least 6 characters**.
- **Purpose separation:** reset codes live in their own table and are hashed with a different domain prefix — a signup OTP can never reset a password, and a reset code can never verify an email.
- **On success** the password hash is replaced, the code is consumed, and the account's **session epoch is bumped — every existing session dies at once** (the owner logs in fresh with the new password). A reset does **not** verify the email: an account still in the signup-OTP stage stays there and meets the OTP screen at next login.

---

## Optional: automatic online payment (Razorpay) — dormant

Built, tested, and **off** until configured. It is NOT the launch model (the panel never shows a checkout button — Farhan collects in person); it exists so a gym could one day pay online and unlock with nobody present.

- `POST /api/subscription/order` creates a Razorpay order for the **server-computed** amount (user id in the order notes); without the keys it answers 404 and the panel stays in QR mode.
- `POST /api/webhooks/razorpay` (public) verifies the **HMAC-SHA256 webhook signature** in constant time; on `payment.captured` / `order.paid` for a known order it records the payment (method `razorpay`) through the normal path, so the gate lifts by itself. **Idempotent** — the same Razorpay payment id never records twice. A bad signature records nothing.

**Farhan's launch steps (numbered):**

1. Create an account at razorpay.com and complete **KYC** (PAN, bank details, business proof).
2. In the Razorpay dashboard → **Settings → API Keys**, generate the live **Key ID + Key Secret**.
3. In **Settings → Webhooks**, add a webhook: URL `https://YOUR-APP-ADDRESS/api/webhooks/razorpay`, subscribe to events **`payment.captured`** and **`order.paid`**, and copy its **webhook secret**.
4. Set the three environment variables — `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` — and restart. Automatic unlock is live.
5. Know the cost plainly: **Razorpay charges about 2% per payment** — on a ₹1,599 Standard month that is roughly ₹32 kept by Razorpay.

---

---

## Run it locally

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** in a browser.

**Demo login** (also shown on the login page while `SHOW_DEMO_HINT` is on):

- Email: `owner@powerhousegym.in`
- Password: `Gym@2026`

On first run the app creates `backend/gym.db` and seeds the demo gym: 48 members (Red List = 32 on first load), 5 checked in today, 6 renewals due, 3 expired.

**Run the tests:**

```bash
cd ..            # project root
.venv/bin/python -m pytest tests/ -q     # or: python -m pytest tests/ -q
```

---

## Configuration (environment variables)

Copy `backend/.env.example` to `.env` or set the variables in your host's dashboard.

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | dev default | Signs the login session cookie. **Always set a long random value in production.** |
| `DATABASE_URL` | `sqlite:///…/backend/gym.db` | Database location. |
| `PORT` | `8000` | Port the server listens on (hosts like Render set this automatically). |
| `COOKIE_SECURE` | auto | Session cookie gets the Secure flag automatically when the site is served over HTTPS. Set `COOKIE_SECURE=1` to force it. |
| `SHOW_DEMO_HINT` | `true` | Shows the demo-login hint on the login page. Set `false` on the public site. |
| `SEED_DEMO` | `true` | Seeds the demo gym + demo account. Set `false` after real data is in use (this also disables the demo reset endpoint). |
| `GOOGLE_CLIENT_ID` | *(empty = off)* | Google OAuth client ID — turns on **Continue with Google** (see *Google sign-in* below). |
| `GOOGLE_CLIENT_SECRET` | *(empty = off)* | Google OAuth client secret. Google sign-in stays off until both this and the client ID are set. |
| `APP_BASE_URL` | `http://127.0.0.1:8000` | Public address of the app, used to build the Google redirect URI `{APP_BASE_URL}/auth/google/callback`. |
| `ADMIN_SECRET` | *(empty = off)* | Farhan's platform secret. Set it and `/control` + the admin API exist; leave it empty and both answer 404. It IS the Control page password — long and random. |
| `SMTP_HOST` / `SMTP_PORT` | *(empty = off)* / `587` | Where OTP emails are sent from. Publish setup: `smtp.gmail.com` + `587`. |
| `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | *(empty)* | The sending mailbox, its App Password (Gmail: create one under Google Account → Security → App passwords), and the From address (usually the same mailbox). OTP email works only when HOST+USER+PASS are all set. |
| `DEV_OTP_ECHO` | `off` | Developer/testing ONLY: when `1`, OTP codes are echoed in API responses and the server log so flows can be tested without email. **Never set this live** — with SMTP unset and echo off, codes exist only inside the hashed database row. |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` | *(empty = off)* | Dormant online-payment gateway — see *Optional: automatic online payment (Razorpay)*. The payment panel never uses it; it only unlocks gyms whose payments Razorpay confirms by webhook. |

Operational extras: `GET /health` reports app + database status; errors are logged to `logs/app.log` at the project root (rotating; clients only ever see friendly messages); on startup the app writes a daily backup copy of the database to `backups/` at the project root (last 7 kept).

---

## Google sign-in

The login and signup pages can offer a white **Continue with Google** button. It is **hidden by default** and appears automatically once the two Google credentials are set — no code change needed.

**One-time setup (Google Cloud Console):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create (or pick) a project.
2. Open **APIs & Services → OAuth consent screen**, choose **External**, and fill in the app name (Do Gym) and your support email.
3. Open **APIs & Services → Credentials → Create Credentials → OAuth client ID**, choose **Web application**.
4. Under **Authorized redirect URIs**, add exactly this line (replace the host with your real public address):
   `https://YOUR-APP-ADDRESS/auth/google/callback`
   For local testing you can also add `http://127.0.0.1:8000/auth/google/callback`.
5. Copy the generated **Client ID** and **Client secret** into the environment as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, and set `APP_BASE_URL` to the same public address you used in step 4 (e.g. `https://your-app.onrender.com`). Restart the app.

**How it works:** tapping the button sends the owner to Google's own consent screen; Google returns them to `/auth/google/callback`. If the Google email matches an existing account that already passed OTP, they are logged straight in; if it is new, an account is created (name taken from the Google profile, no password) and — exactly like an email signup — it first passes the **OTP stage** (below), and only then continues to Gym & Owner Details. Accounts created via Google are badged **"Google ✓ verified"** on Farhan's Control page (email signups show an **"Email"** tag). Only **verified** Google emails are accepted, and the sign-in link is protected by a signed, 10-minute **state** value. Any problem — cancelled consent, expired link, Google unreachable — returns to the login page with a friendly message, never an error screen.

---

## QR self check-in (members scan, the owner does nothing)

Check-in is **fully automatic**. The QR poster on the gym wall encodes the gym's personal check-in link, `APP_BASE_URL/checkin/<token>` — the token is an unguessable per-gym secret stored in the settings table (generated automatically; the owner sees the link, a Copy button and a *New Link* button on the QR Check-In screen — *New Link* retires every previously printed poster).

1. A member scans the poster with **their own phone camera** and lands on a public page (no login) showing the gym's name.
2. **First visit only:** they type their personal member code (`PHG-001`, `PHG-002`, … — every member has one; it is printed on their membership card and shown next to their name everywhere in the owner's app). The phone remembers the code.
3. **Every later visit:** the page submits by itself the moment it opens — scan, and you're in. A *“Not you?”* link on the result card clears the remembered code.
4. The server writes the attendance row (member, date, entry time, source `qr`). The owner's dashboard counts, No-Show Red List and Today's Check-Ins read the same table, so they update on their own — the owner never touches anything. A repeat scan the same day is idempotent: it answers *“Already checked in at \<time\>”* instead of writing a second row.

Member codes are assigned by the server: `POST /api/members` (owner-only) always issues the **next sequential PHG number** from a monotonic counter, so codes are unique forever — a deleted member's number is never reissued — and any member row found without a code is backfilled at startup. Scan attempts are rate-limited per IP + gym (30/min), a code only ever matches this gym's own members, and the public page shows nothing beyond the gym's name.

**Important:** members' phones can only reach the check-in page once the site is deployed at a **public URL**, and `APP_BASE_URL` must be set to that public address — the printed QR encodes whatever `APP_BASE_URL` says, so set it *before* printing posters. Owner-side check-in by member code stays available in the app as a backup.

---

## Security notes

The full plain-language summary for Farhan lives in **[SECURITY.md](SECURITY.md)** (cross-account data lock, face-gated unlocking, sessions, headers, rate limits — and what he must set at publish). The short technical list:

- Passwords are hashed with **bcrypt**; sessions are **signed, HTTP-only cookies** (itsdangerous), 7-day expiry.
- Login is **rate-limited** (5 attempts/minute per IP) with a friendly wait message. Sign-up is rate-limited too.
- Every API route except the public pages, plans and config requires login — logged-out calls return **401**.
- **Security headers** on every response: Content-Security-Policy, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, Referrer-Policy.
- All input is validated server-side (Pydantic + friendly field-by-field messages); database access is via SQLAlchemy ORM (no string-built SQL); the frontend escapes all member data before rendering (XSS-safe).
- Legal pages with real policy text: **/privacy** and **/terms**, linked from the login page and the app footer.

---

## Going live (numbered steps)

1. **Push this project to GitHub** (the whole `gym-app` folder as one repository).
2. **Create a free account on Render** (render.com) and choose **New → Blueprint**; select your GitHub repository. Render reads `render.yaml` automatically (a `Procfile` is included as a backup for other hosts).
3. **Set the environment variables** when Render asks: it generates a `SECRET_KEY` for you; `SHOW_DEMO_HINT` is already set to `false` and `SEED_DEMO` to `true` in `render.yaml` — keep them that way for the public demo.
4. **Wait for the build to finish** and open the live URL Render gives you (like `https://your-app.onrender.com`). Log in with the demo account to check everything works.
5. **Change the demo password before real use** — the seeded demo account (`owner@powerhousegym.in` / `Gym@2026`) is public knowledge from this README. Before handling real member data, set `SEED_DEMO=false`, remove the demo account from the database, and create your own account via the Sign Up page.
6. **Know the free-plan limit:** Render's free plan has an **ephemeral disk** — the SQLite database resets when the service redeploys or restarts. For a real gym, upgrade to a paid Render plan with a **persistent disk** (or move `DATABASE_URL` to a hosted Postgres) so member data is never lost.
7. **Payments & your control:** (a) make sure `backend/static/payment-qr.png` holds **your** PhonePe QR (it does); (b) set `ADMIN_SECRET` so your `/control` page exists; (c) set the five `SMTP_*` variables so signup OTPs actually email (a Gmail App Password works); (d) open the live site on a device with a camera, go to `/control`, and **enroll your face** — the gym unlock (Pay button) and Control's Activate both need it, and browsers allow camera access only on the live HTTPS address. *Optional, later:* automatic online payment via Razorpay — see *Optional: automatic online payment (Razorpay)* below.

Questions during setup: every step above uses only the GitHub and Render websites — no commands are needed after step 1.

---

## Project layout

```
gym-app/
├── backend/
│   ├── main.py          # FastAPI app: API, auth, OTP, face unlock, gates, /control, security headers
│   ├── billing.py       # Subscription plans (single source of truth)
│   ├── models.py        # SQLAlchemy models (users, members, check-ins, payments, face template, OTP, …)
│   ├── database.py      # SQLite engine/session (DATABASE_URL env)
│   ├── security.py      # bcrypt + signed session cookies
│   ├── seed.py          # Demo data (idempotent — restarts never duplicate)
│   ├── member_codes.py  # PHG member-code helpers (monotonic counter)
│   ├── payments_razorpay.py # Dormant Razorpay engine (env-gated; order + webhook)
│   ├── static/          # payment-qr.png — Farhan's UPI QR + face/ — vendored face-api.js + model weights (no CDN)
│   ├── requirements.txt # Pinned dependencies
│   └── .env.example
├── frontend/            # index.html (app), pricing.html, privacy.html, terms.html, 404.html
├── tests/               # pytest suite (API, flows, plans, gate, admin/control, face, OTP, security) + VERIFICATION.md
├── SECURITY.md          # Plain-language security summary
├── render.yaml          # One-click Render blueprint
├── Procfile
├── README.md
└── VERIFICATION.md      # Every check run against the live server, with outputs
```
