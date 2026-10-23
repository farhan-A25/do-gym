# Do Gym — Security, in plain words

*For Farhan. Last updated 2026-10-23. Every protection below is switched ON in the real app and covered by automated attack tests (`tests/test_security.py`, plus the admin/face/OTP suites).*

## The one-sentence version

Nobody can read another gym's money details, nobody can unlock a gym without paying (or without YOUR face), passwords and codes are stored scrambled, and the server never shows its insides to a visitor.

## Who can see what

- **Members' register** (names, check-ins, renewals) belongs to the gym. One installed copy of Do Gym serves **one gym**, so the owner accounts on that copy are that gym's own team and share the register — like a register book on the gym desk.
- **Money is private per account.** A gym's payments — amounts, dates, how they paid — are visible ONLY to that gym's account. One account can never see another account's payment, even on the same copy. (This was audited line-by-line; a second account used to be able to ride on the first account's payment to get in free. That hole is closed and tested.)
- **Your Control page** (`/control`) does not exist at all until you set `ADMIN_SECRET` when publishing. It is never linked from the app. It needs your password, locks after wrong tries, and every action on it needs a secret form token.
- **The database file, backups and logs cannot be downloaded** from the website — those addresses simply answer "not found".

## Unlocking a gym — "no face, no unlock"

- A gym unlocks ONLY when a payment is recorded for THAT gym.
- Recording a payment needs **your face**: the owner's **Pay** button starts a face scan on the owner's device; your Control **Activate** button starts the same scan on your device. A scan checks (1) the face matches the template you enrolled, and (2) a small live movement (blink / head turn) chosen by the server at that moment, so a photo can't easily fool it. Without a passed scan, **nothing is recorded** — this is enforced on the server, not just hidden in the page.
- There is exactly **one exception**, on purpose: the secret-header admin API (`X-Admin-Secret`), a break-glass for scripts/emergencies. No screen links to it. Keep the secret like a master key.
- **Switch Off** in Control locks a gym instantly — even mid-paid-period — and deletes nothing. Only your face-verified Activate (or the break-glass) turns it back on.
- Honest limit, your informed choice: face matching runs in the browser, so it is **convenience-grade, not bank-grade**. The server-side challenge, liveness, attempt lockouts (5 fails → 15 minutes) and switch-off make casual cheating hard, but a determined technical attacker with the owner's logged-in device is not fully stopped by this layer. The money itself still moves hand-to-hand with you.

## Passwords, OTPs and sessions

- Passwords are **bcrypt-hashed (cost 12)** — the database never holds a readable password.
- Signup OTPs are **stored hashed**, live **10 minutes**, allow **5 tries**, and a resend waits **60 seconds**. In production an OTP is **never** written to a log or an API response — it only travels by email (SMTP). The `DEV_OTP_ECHO` flag that shows codes to developers is OFF unless someone deliberately turns it on for local testing. Never turn it on live.
- Login sessions are signed, **HTTP-only cookies** (7 days), marked **Secure** automatically on HTTPS. **Every login mints a fresh token**, and **logging out kills ALL previously issued tokens** for that account at once — so a copied cookie stops working the moment the owner logs out anywhere.
- **Rate limits with lockouts** guard every door: login & signup (5/minute), Control login (5/minute), OTP (above), face scans (5 fails → 15 minutes), member scans (30/minute).

## The website's armour (automatic, on every response)

- **Content-Security-Policy** — pages can only load scripts/styles from approved places; the site cannot be framed by another site (`frame-ancestors 'none'` + `X-Frame-Options: DENY`) — clickjacking is blocked.
- **Permissions-Policy** — camera allowed ONLY for this site itself (the face scan needs it); microphone, location and sensors are off.
- **HSTS** — once a browser has visited over HTTPS, it refuses plain HTTP for a year.
- **CSRF protection** — a state-changing request that arrives from a *different* website's origin is refused; Control forms additionally demand their secret token.
- **No error leaks** — if something crashes, the visitor sees "Something went wrong. Please try again." The details go only to the server log, and even there **emails are masked** (`ow***@…`) and phone numbers starred.
- **All database access is parameterized** (SQLAlchemy ORM) — the classic `' OR '1'='1` injection probes are inert and tested.
- Google sign-in accepts only **verified** Google emails and uses a signed, 10-minute state token; Razorpay's webhook (dormant until you enable it) verifies an **HMAC signature** and records each payment **once**, even if delivered twice.

## Your part (the short publish checklist)

1. Set a strong `SECRET_KEY` and `ADMIN_SECRET` at deploy (Render generates/asks — never put them in the repo or chat).
2. Set the five `SMTP_*` values so OTP emails actually send.
3. Keep `DEV_OTP_ECHO` OFF and `SHOW_DEMO_HINT=false` live; change the demo password / set `SEED_DEMO=false` before real members arrive.
4. Enroll your face in Control from a device with a camera, on the live (HTTPS) address — browsers only allow camera access on HTTPS.
5. Never share your Control password; there is no "forgot" link by design — the break-glass API with `ADMIN_SECRET` is the recovery path.
