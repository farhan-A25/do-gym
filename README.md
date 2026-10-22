# Do Gym

A complete gym-owner web app that stops members from silently quitting. The owner sees **who has stopped coming** (the No-Show Red List), checks members in by **QR code**, handles **renewals** before they lapse, and sells **add-ons** (personal training, diet plans, and more) — all from one premium dashboard in a black & white + blue theme (Meta Muse style; the No-Show Red List keeps its red danger accents).

Built as a real full-stack website: **FastAPI + SQLite backend** with a secure owner login, and a mobile-style frontend served by the same app.

---

## The Screens

| Screen | What it does |
|---|---|
| **Home / Dashboard** | Live stats (Total Members, Checked In Today, Red List count, Renewals Due), member quick-find search with a status card (last visit, plan/expiry, add-ons, Call / Check-In buttons), AUTO/MANUAL mode card, current-plan strip, and Today's Check-Ins. |
| **Screen 1 — No-Show Red List** | Members absent 10+ days, biggest absences first, with a red call button per member. Filter cycles 10+ / 15+ / 20+ days. In Manual mode the owner can flag or clear members by hand. |
| **Screen 2 — QR Check-In** | The gym's QR code, check-in by member code (e.g. `PHG-001`), a Simulate Scan button, and every member's personal QR code. Duplicate check-ins on the same day are politely rejected. |
| **Screen 3 — Renewal** | Memberships expiring within 7 days and expired memberships, each with a Renew button (Manual mode: extend by a custom number of days), plus renewal history and "Renewed this month" revenue. |
| **Screen 4 — Add-Ons** | Catalog (Personal Training ₹5,000/mo, Diet Plan ₹1,500/mo, Steam & Sauna ₹999/mo, Group Classes ₹1,999/mo, Locker ₹499/mo, Protein Pack ₹2,499/mo), assign/remove per member, live add-on revenue. |
| **Plan & Billing** | The gym's current subscription, member usage, plan switching paid by **UPI QR** (payment panel with amount + QR, *I Have Paid* / *Pay Later*, payment status and last payment), and Settings (Gym & Owner Details). |

**Entry flow for a new gym:** Sign Up → Gym & Owner Details → Choose a Plan → Dashboard.
The demo account already completed the flow, so it goes straight to the Dashboard.

**AUTO / MANUAL modes:** In AUTO (default), every list, count and revenue figure updates itself from the database. In MANUAL, automatic highlights pause and the owner flags/clears the Red List, marks attendance by tapping names, and extends renewals by custom days. Manual flags are stored separately and respected by the automatic logic in every section.

**Search everywhere:** Every section has a live search bar (name or member code, case-insensitive), with "Showing X" counts and a friendly "No member found" state.

Everything is connected through **one source of truth — the database**: a check-in removes the member from the Red List and updates Home instantly; a renewal updates counts, history and revenue everywhere; an add-on shows on the member's profile in every section.

---

## Subscription Plans (for using this website)

Plans are defined once in the backend (`backend/billing.py`) and served via `GET /api/plans`; the public `/pricing` page and the in-app Plan & Billing screen both render from it.

| Plan | Monthly | Yearly (2 months free) | Includes |
|---|---|---|---|
| **Standard** | ₹1,299/mo | ₹12,990/yr | Up to 150 members, Red List, QR Check-In, Renewals, search everywhere, AUTO mode, 1 owner login |
| **Premium** ⭐ Most Popular | ₹1,899/mo | ₹18,990/yr | Unlimited members, everything in Standard + Add-Ons & revenue, AUTO + MANUAL modes, renewal history & reports, priority support |

*Prices in INR. GST extra as applicable. Cancel anytime.*
Market basis: Indian gym software competitors run ₹399–₹1,999/month (GymMunshi, Fitzenix, GymExec, FitnessDesk), and yearly billing across the market works out to about 2 months free.

**Paying by UPI QR:** choosing a plan (in the entry flow's Upgrade step, or later in Plan & Billing) opens a payment panel showing the owner's UPI QR code, the exact amount taken from `billing.py`, and two buttons:

- **I Have Paid** — the plan activates immediately. The payment is recorded (plan, period, amount, method `upi_qr`, status *active*, date) and Plan & Billing shows *"Paid via UPI QR · ₹… · Active since …"* plus the last payment.
- **Pay Later** — the plan is saved with status *payment_pending*: Plan & Billing shows an amber **Payment pending** badge and a **Pay Now** button that re-opens the panel. The app remains fully usable meanwhile.

Amounts are **always computed server-side from `billing.py`** — any amount a browser sends is ignored — and every payment (paid or pending) is written to the server audit log (`logs/app.log`). The demo gym starts on Premium (monthly) with no payment recorded; its seeded status reads "active (demo billing)" until the first QR payment is made.

**The payment QR is one file:** the owner's real PhonePe UPI QR is already installed at **`backend/static/payment-qr.png`** and is served inside the logged-in app at `GET /payment-qr.png` (logged-out requests get 401). **Replace `backend/static/payment-qr.png` with your own UPI QR image — nothing else to change.** A full poster version is kept alongside as `backend/static/payment-qr-poster.png` (not used by the app). If the QR file is ever missing, the route serves a simple generated placeholder image and the panel falls back to a "Payment QR coming soon" box — it never shows a broken image.

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

On first run the app creates `backend/gym.db` and seeds the demo gym: 48 members (Red List = 32 on first load), 5 checked in today, 6 renewals due, 3 expired, ₹8,499/month of add-on revenue.

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

Operational extras: `GET /health` reports app + database status; errors are logged to `logs/app.log` at the project root (rotating; clients only ever see friendly messages); on startup the app writes a daily backup copy of the database to `backups/` at the project root (last 7 kept).

---

## Security notes

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
7. **Payments:** subscriptions are paid by **UPI QR** out of the box (see *Subscription Plans* above) — before real customers sign up, make sure `backend/static/payment-qr.png` holds **your** UPI QR (the installed one is the owner's PhonePe QR). *Optional, later:* for automatic payment verification instead of the owner's "I Have Paid" tap, create a Razorpay account, complete KYC, and wire Razorpay Checkout/Subscriptions into the `POST /api/subscription/confirm-payment` step (`RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` as environment variables).

Questions during setup: every step above uses only the GitHub and Render websites — no commands are needed after step 1.

---

## Project layout

```
gym-app/
├── backend/
│   ├── main.py          # FastAPI app: API, auth, pages, security headers
│   ├── billing.py       # Subscription plans (single source of truth)
│   ├── models.py        # SQLAlchemy models (users, members, check-ins, …)
│   ├── database.py      # SQLite engine/session (DATABASE_URL env)
│   ├── security.py      # bcrypt + signed session cookies
│   ├── seed.py          # Demo data (idempotent — restarts never duplicate)
│   ├── static/          # payment-qr.png — the owner's UPI payment QR (one swappable file) + poster copy
│   ├── requirements.txt # Pinned dependencies
│   └── .env.example
├── frontend/            # index.html (app), pricing.html, privacy.html, terms.html, 404.html
├── tests/               # pytest suite (API, flows, plans, UPI QR payments, entry journey)
├── render.yaml          # One-click Render blueprint
├── Procfile
├── README.md
└── VERIFICATION.md      # Every check run against the live server, with outputs
```
