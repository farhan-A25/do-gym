"""PowerHouse Gym — Owner App (FastAPI backend).

Run from this folder:
    pip install -r requirements.txt
    uvicorn main:app --port 8000
Then open http://localhost:8000 and log in with the demo owner account
(see README.md).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape as html_escape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

import models
import payments_razorpay
from billing import SUBSCRIPTION_PLANS, get_subscription_plan
from database import Base, SessionLocal, engine, get_db
from google_auth import (
    app_base_url,
    build_authorize_url,
    exchange_code_for_token,
    fetch_google_userinfo,
    google_auth_enabled,
)
from member_codes import (
    checkin_token_matches,
    ensure_member_codes,
    get_or_create_checkin_token,
    next_member_code,
    reset_checkin_token,
)
from seed import PLANS, seed_database, seed_if_empty
from security import (
    ADMIN_COOKIE,
    ADMIN_MAX_AGE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_admin_token,
    create_csrf_token,
    create_oauth_state,
    create_session_token,
    hash_password,
    read_admin_token,
    read_session_token,
    verify_csrf_token,
    verify_oauth_state,
    verify_password,
)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
STATIC_DIR = BASE_DIR / "static"
PAYMENT_QR_FILE = STATIC_DIR / "payment-qr.png"
LOG_DIR = BASE_DIR.parent / "logs"
BACKUP_DIR = BASE_DIR.parent / "backups"

# ---------------------------------------------------------------------------
# Logging: everything important goes to logs/app.log (rotating) AND to
# stderr (the host's log stream), nothing silent
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("powerhouse")
logger.setLevel(logging.INFO)


class _RedactFilter(logging.Filter):
    """Scrub personal data out of every log line: email addresses are
    masked (ow***@domain) and 10-digit phone numbers are starred. OTP
    codes are never passed to the logger outside the dev echo path
    (see _deliver_otp), so they never reach a log file in production."""

    _EMAIL = re.compile(r"([\w.+-]{1,2})[\w.+-]*@([\w-]+(?:\.[\w-]+)+)")
    _PHONE = re.compile(r"\b\d{10}\b")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = self._EMAIL.sub(r"\1***@\2", msg)
            redacted = self._PHONE.sub("**********", redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3)
    _handler.setFormatter(_formatter)
    _handler.addFilter(_RedactFilter())
    logger.addHandler(_handler)
    # Mirror everything to stderr as well: the file alone is invisible
    # to the host's log stream (Render shows the process output), which
    # made mail delivery fail silently from outside — sent / failed /
    # not-configured lines must be visible without shell access to the
    # box. Only THIS logger gets the extra handler (uvicorn's own
    # loggers are untouched), so its access logs are not duplicated.
    _console = logging.StreamHandler(sys.stderr)
    _console.setFormatter(_formatter)
    _console.addFilter(_RedactFilter())
    logger.addHandler(_console)
logger.addFilter(_RedactFilter())


def daily_backup() -> None:
    """Keep one SQLite backup copy per day in backups/ (last 7 kept).

    Runs as a startup check: if today's backup does not exist yet, make it.
    """
    try:
        db_file = BASE_DIR / "gym.db"
        if not db_file.exists():
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        today_name = f"gym-{date.today().isoformat()}.db"
        target = BACKUP_DIR / today_name
        if not target.exists():
            shutil.copy2(db_file, target)
            logger.info("Daily backup created: %s", today_name)
        backups = sorted(BACKUP_DIR.glob("gym-*.db"))
        for old in backups[:-7]:
            old.unlink()
    except Exception:
        logger.exception("Daily backup failed")


Base.metadata.create_all(bind=engine)
# Lightweight upgrade for databases created before CheckIn.source existed
# (create_all never alters an existing table).
if "source" not in [c["name"] for c in sa_inspect(engine).get_columns("checkins")]:
    with engine.begin() as _conn:
        _conn.execute(sa_text(
            "ALTER TABLE checkins ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'owner'"
        ))
# Same lightweight upgrade for the Razorpay columns on subscription
# payments (create_all adds the NEW gateway_orders table by itself, but
# never alters this existing one).
_sub_pay_cols = [c["name"] for c in sa_inspect(engine).get_columns("subscription_payments")]
with engine.begin() as _conn:
    if "gateway_order_id" not in _sub_pay_cols:
        _conn.execute(sa_text(
            "ALTER TABLE subscription_payments ADD COLUMN gateway_order_id VARCHAR(64)"
        ))
    if "gateway_payment_id" not in _sub_pay_cols:
        _conn.execute(sa_text(
            "ALTER TABLE subscription_payments ADD COLUMN gateway_payment_id VARCHAR(64)"
        ))
# Same lightweight upgrade for the users table: auth provider (for the
# admin control page's Google/Email badge) and the joined date.
_user_cols = [c["name"] for c in sa_inspect(engine).get_columns("users")]
with engine.begin() as _conn:
    if "auth_provider" not in _user_cols:
        _conn.execute(sa_text(
            "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(16) NOT NULL DEFAULT 'email'"
        ))
    if "created_at" not in _user_cols:
        _conn.execute(sa_text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
        _conn.execute(
            sa_text("UPDATE users SET created_at = :now WHERE created_at IS NULL"),
            {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")},
        )
    if "otp_pending" not in _user_cols:
        # Existing accounts predate the OTP stage — they stay verified
        # (default 0); only brand-new signups start pending.
        _conn.execute(sa_text(
            "ALTER TABLE users ADD COLUMN otp_pending BOOLEAN NOT NULL DEFAULT 0"
        ))
    if "session_epoch" not in _user_cols:
        _conn.execute(sa_text(
            "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0"
        ))
SEED_ENABLED = os.environ.get("SEED_DEMO", "true").strip().lower() != "false"
with SessionLocal() as _db:
    if SEED_ENABLED:
        seed_if_empty(_db)
    # Every member must hold a PHG code, and the gym must hold a check-in
    # token for its wall QR poster — backfill both on every boot (no-ops
    # once present).
    _backfilled = ensure_member_codes(_db)
    if _backfilled:
        logger.info("Backfilled PHG member codes for %d member(s)", _backfilled)
    get_or_create_checkin_token(_db)
daily_backup()
logger.info("PowerHouse Gym backend started (seed_demo=%s)", SEED_ENABLED)

app = FastAPI(title="PowerHouse Gym Owner App", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Never leak a stack trace: log it server-side, answer politely."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our side. Please try again in a moment."},
    )


@app.get("/health")
def health():
    """App + database status. Public, safe (no data exposed)."""
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception:
        logger.exception("Health check: database unreachable")
        db_status = "error"
    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status}


@app.get("/api/config")
def public_config():
    """Public, non-sensitive UI configuration (no user data)."""
    show_hint = os.environ.get("SHOW_DEMO_HINT", "true").strip().lower() != "false"
    return {"show_demo_hint": show_hint}

# ---------------------------------------------------------------------------
# Security headers on every response
# ---------------------------------------------------------------------------
CSP = (
    "default-src 'self'; "
    # checkout.razorpay.com serves Razorpay Checkout (loaded only when
    # the gateway is configured); api.razorpay.com is its iframe/API.
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://checkout.razorpay.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com; "
    "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = CSP
    # Camera is allowed ONLY for this site itself (face unlock +
    # enrollment); microphone, location and sensors are off entirely.
    response.headers["Permissions-Policy"] = (
        "camera=(self), microphone=(), geolocation=(), usb=(), "
        "magnetometer=(), gyroscope=(), accelerometer=()"
    )
    if _request_is_https(request):
        # HSTS only makes sense over HTTPS (Render terminates TLS and
        # forwards the proto) — sending it over plain HTTP is ignored
        # by browsers and would be wrong on a local dev box.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


def _request_is_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        or os.environ.get("COOKIE_SECURE", "") == "1"
    )


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    """CSRF defense-in-depth for the owner API. The session cookie is
    SameSite=Lax and every state-changing endpoint speaks JSON (which
    a cross-site form cannot produce without a CORS preflight — and
    no CORS is enabled), and on top of that: a non-GET /api call that
    arrives WITH an Origin header naming a DIFFERENT host is refused
    outright. Server-to-server callers (no Origin) are unaffected;
    the Razorpay webhook (signature-verified instead) and the admin
    endpoints (secret header / token-protected forms) are exempt."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        path = request.url.path
        if (
            path.startswith("/api/")
            and not path.startswith("/api/webhooks/")
            and not path.startswith("/api/admin/")
            and not path.startswith("/api/control/")
        ):
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlparse(origin).netloc
                if origin_host and origin_host != request.headers.get("host", ""):
                    logger.warning(
                        "CSRF origin check refused %s %s (Origin host %s)",
                        request.method, path, origin_host,
                    )
                    return _gate_block_response(
                        403, {"detail": "Cross-site request refused."}, request
                    )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pay-first gate (owner order, Part 2, 2026-10-23): an authenticated owner
# whose subscription is not ACTIVE gets NO working app. Only the entry
# flow and the payment step stay reachable:
#   * details step .......... /api/details (GET/PUT)
#   * plans + payment ....... /api/plans, /api/plan, /api/plan/switch,
#                             /api/subscription/order (dormant Razorpay
#                             plumbing), /payment-qr.png (the QR image)
#   * session ............... /api/me, /api/logout, login/signup, Google
#                             auth, /api/auth/config, /api/config
# Everything else under /api/ answers 402 with code "payment_required"
# and the frontend routes to the payment step. Non-API routes (the SPA
# shell at /, legal pages, icons — and /control, Farhan's admin page,
# which does its own password auth) are served as usual — the SPA
# itself performs the redirect to the payment view. The /api/admin/*
# endpoints also pass through here: they authenticate themselves with
# the ADMIN_SECRET, never with an owner session. Requests with no valid
# session pass through untouched (endpoints answer their usual 401).
# The PUBLIC member check-in (/checkin/<token>, /api/checkin/scan) is
# never gated: members keep scanning regardless of the owner's billing.
# An account Farhan has SWITCHED OFF from Control counts as not active
# here even inside a paid window (see subscription_info's override).
# ---------------------------------------------------------------------------
GATE_OPEN_PATHS = {
    "/health",
    "/api/config", "/api/plans", "/api/auth/config",
    "/auth/google", "/auth/google/callback",
    "/api/login", "/api/signup", "/api/logout", "/api/me",
    "/api/auth/forgot", "/api/auth/reset",
    "/api/details", "/api/plan", "/api/plan/switch",
    "/api/subscription/order",
    "/api/face-unlock/template",
    "/api/face-unlock/challenge",
    "/api/face-unlock/verify",
    "/api/webhooks/razorpay", "/payment-qr.png",
    "/api/checkin/scan",
}


OTP_OPEN_PATHS = {"/api/me", "/api/logout", "/api/config", "/api/auth/config"}


def _gate_block_response(status_code: int, content: dict, request: Request = None) -> JSONResponse:
    """A gate refusal. Sets the security headers inline because this
    response short-circuits before the headers middleware on some paths."""
    response = JSONResponse(status_code=status_code, content=content)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Permissions-Policy"] = (
        "camera=(self), microphone=(), geolocation=(), usb=(), "
        "magnetometer=(), gyroscope=(), accelerometer=()"
    )
    if request is not None and _request_is_https(request):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.middleware("http")
async def subscription_gate(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/api/admin/")
        or path.startswith("/api/control/")
        or path.startswith("/checkin/")
        or not path.startswith("/api/")
    ):
        return await call_next(request)
    payload = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not payload:
        return await call_next(request)  # not logged in -> endpoints 401
    with SessionLocal() as db:
        user = (
            db.query(models.User)
            .filter(models.User.id == payload.get("uid"))
            .first()
        )
        if user is None:
            return await call_next(request)
        # Canonical flow, stage 2: until the email OTP is entered,
        # EVERYTHING except the OTP endpoints themselves (and a small
        # status set) is blocked — details, payment and the app
        # alike. This runs before the payment gate below.
        if getattr(user, "otp_pending", False):
            if path in OTP_OPEN_PATHS or (
                path.startswith("/api/auth/") and request.method != "GET"
            ):
                return await call_next(request)
            logger.info(
                "OTP gate blocked %s %s for %s (OTP not yet entered)",
                request.method, path, user.email,
            )
            return _gate_block_response(403, {
                "detail": "Please enter the OTP we sent to your email first.",
                "code": "otp_required",
            }, request)
        info = subscription_info(db, user_id=user.id)
    if path in GATE_OPEN_PATHS:
        return await call_next(request)
    if info["active"]:
        return await call_next(request)
    logger.info(
        "Pay-first gate blocked %s %s for %s (subscription %s)",
        request.method, path, user.email, info["status"],
    )
    return _gate_block_response(402, {
        "detail": "Your subscription is not active. "
                  "Please complete your payment to use Do Gym.",
        "code": "payment_required",
        "subscription_status": info["status"],
    }, request)


# ---------------------------------------------------------------------------
# Login rate limiting: max 5 attempts / minute / IP (in-memory)
# ---------------------------------------------------------------------------
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_LIMIT = 5
LOGIN_WINDOW = 60.0


def login_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= LOGIN_LIMIT:
        return True
    attempts.append(now)
    return False


# ---------------------------------------------------------------------------
# Public QR scan rate limiting: max 30 attempts / minute / (IP + gym token).
# Generous for a busy gym door (members often share one network), strict
# enough to stop code guessing.
# ---------------------------------------------------------------------------
_scan_attempts: dict[str, list[float]] = defaultdict(list)
SCAN_LIMIT = 30
SCAN_WINDOW = 60.0


def scan_rate_limited(key: str) -> bool:
    now = time.time()
    attempts = [t for t in _scan_attempts[key] if now - t < SCAN_WINDOW]
    _scan_attempts[key] = attempts
    if len(attempts) >= SCAN_LIMIT:
        return True
    attempts.append(now)
    return False


# ---------------------------------------------------------------------------
# Schemas (Pydantic validation on every endpoint)
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class CheckInIn(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    member_id: int | None = None


class ScanIn(BaseModel):
    """A member self check-in from the public QR page."""
    token: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32)


class MemberCreateIn(BaseModel):
    """Add a member. There is deliberately NO `code` field: the member's
    PHG code is always assigned by the server (next sequential number)."""
    name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=20)
    plan: str = Field(default="", max_length=20)
    expiry_date: str = Field(default="", max_length=10)  # optional YYYY-MM-DD


class RenewIn(BaseModel):
    custom_days: int | None = Field(default=None, ge=1, le=3650)


class ModeIn(BaseModel):
    mode: str = Field(pattern="^(auto|manual)$")


class MemberRefIn(BaseModel):
    member_id: int


class PlanSwitchIn(BaseModel):
    plan_id: str = Field(pattern="^(starter|pro)$")
    period: str = Field(pattern="^(monthly|yearly)$")


class SubscriptionPayIn(BaseModel):
    """Plan + period chosen for the UPI QR payment. Any amount a client
    sends is deliberately NOT part of this schema — the amount is always
    computed server-side from billing.py."""
    plan_id: str = Field(pattern="^(starter|pro)$")
    period: str = Field(pattern="^(monthly|yearly)$")


class SignupIn(BaseModel):
    owner_name: str = Field(default="", max_length=120)
    gym_name: str = Field(default="", max_length=160)
    email: str = Field(default="", max_length=255)
    mobile: str = Field(default="", max_length=20)
    password: str = Field(default="", max_length=200)


class DetailsIn(BaseModel):
    gym_name: str = Field(default="", max_length=160)
    gym_address: str = Field(default="", max_length=255)
    gym_city: str = Field(default="", max_length=80)
    gym_phone: str = Field(default="", max_length=20)
    opening_year: int | None = None
    owner_name: str = Field(default="", max_length=120)
    mobile: str = Field(default="", max_length=20)
    whatsapp: str = Field(default="", max_length=20)


# ---------------------------------------------------------------------------
# Friendly validation helpers (field-by-field messages, never raw errors)
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_phone(raw: str) -> str:
    """Normalise an Indian phone number to 10 digits where possible."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def valid_mobile(raw: str) -> bool:
    return len(clean_phone(raw)) == 10


def field_error(fields: dict, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": message, "fields": fields})


def user_payload(user: models.User) -> dict:
    return {
        "email": user.email,
        "owner_name": user.owner_name or "",
        "gym_name": user.gym_name or "",
        "details_completed": bool(user.details_completed),
        "plan_chosen": bool(user.plan_chosen),
        # Canonical flow stage: True between account creation and the
        # email OTP being entered (details/payment/app stay blocked).
        "otp_pending": bool(getattr(user, "otp_pending", False)),
    }


def details_payload(user: models.User) -> dict:
    return {
        "gym_name": user.gym_name or "",
        "gym_address": user.gym_address or "",
        "gym_city": user.gym_city or "",
        "gym_phone": user.gym_phone or "",
        "opening_year": user.opening_year,
        "owner_name": user.owner_name or "",
        "mobile": user.mobile or "",
        "whatsapp": user.whatsapp or "",
        "email": user.email,
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _session_epoch(db: Session, user_id: int) -> int:
    """The account's current session epoch (logout bumps it, killing
    every previously issued session token for that account). Stored
    on the user row itself, so demo-data resets never disturb it."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        return 0
    return int(getattr(user, "session_epoch", 0) or 0)


def current_user(request: Request, db: Session = Depends(get_db)):
    """Return the logged-in User, or a 401 JSONResponse (endpoints check for it)."""
    payload = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not payload:
        return _unauthorized()
    user = db.query(models.User).filter(models.User.id == payload.get("uid")).first()
    if not user:
        return _unauthorized()
    if int(payload.get("epoch", 0) or 0) != _session_epoch(db, user.id):
        return _unauthorized()  # logged out since this token was issued
    return user


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Not logged in."})


def err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


# ---------------------------------------------------------------------------
# Derived-data helpers (everything is computed from the database, always)
# ---------------------------------------------------------------------------
def last_visit(db: Session, member_id: int) -> date | None:
    row = (
        db.query(models.CheckIn)
        .filter(models.CheckIn.member_id == member_id)
        .order_by(models.CheckIn.date.desc(), models.CheckIn.id.desc())
        .first()
    )
    return row.date if row else None


def days_absent(db: Session, member: models.Member, today: date) -> int:
    lv = last_visit(db, member.id)
    if lv is None:
        return 9999
    return (today - lv).days


def checked_in_today(db: Session, member_id: int, today: date) -> bool:
    return (
        db.query(models.CheckIn)
        .filter(models.CheckIn.member_id == member_id, models.CheckIn.date == today)
        .first()
        is not None
    )


def member_dict(db: Session, m: models.Member, today: date) -> dict:
    lv = last_visit(db, m.id)
    return {
        "id": m.id,
        "code": m.code,
        "name": m.name,
        "phone": m.phone,
        "plan": m.plan,
        "expiry_date": m.expiry_date.isoformat(),
        "days_left": (m.expiry_date - today).days,
        "last_visit": lv.isoformat() if lv else None,
        "days_absent": days_absent(db, m, today),
        "checked_in_today": checked_in_today(db, m.id, today),
    }


def flag_ids(db: Session) -> set[int]:
    return {r.member_id for r in db.query(models.ManualFlag).all()}


def clear_ids(db: Session) -> set[int]:
    return {r.member_id for r in db.query(models.ManualClear).all()}


def red_list(db: Session, today: date, min_days: int = 10) -> list[dict]:
    """Computed no-show list: (10+ days absent OR manually flagged) minus manual clears."""
    flags, clears = flag_ids(db), clear_ids(db)
    rows = []
    for m in db.query(models.Member).all():
        da = days_absent(db, m, today)
        is_flagged = m.id in flags
        if m.id in clears:
            continue
        if da >= min_days or is_flagged:
            d = member_dict(db, m, today)
            d["manual"] = is_flagged
            rows.append(d)
    rows.sort(key=lambda r: (-r["days_absent"], r["id"]))
    return rows


def get_mode(db: Session) -> str:
    row = db.query(models.Setting).filter(models.Setting.key == "mode").first()
    return row.value if row else "auto"


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if not row:
        db.add(models.Setting(key=key, value=value))
    else:
        row.value = value


def payment_dict(p: models.SubscriptionPayment) -> dict:
    return {
        "plan_id": p.plan_id,
        "plan_name": p.plan_name,
        "period": p.period,
        "amount": p.amount,
        "method": p.method,
        "status": p.status,  # paid | pending
        "date": p.created_at.date().isoformat() if p.created_at else "",
    }


# ---------------------------------------------------------------------------
# Subscription state — the pay-first gate's single source of truth
# (owner order, Part 2, 2026-10-23: PAY FIRST, THEN THE WEBSITE WORKS).
#
# ACTIVE means the gym has paid and the paid period has not ended:
#   * the latest subscription payment is "paid" and today is inside its
#     billing window (monthly = 30 days, yearly = 365 days, counted from
#     the payment date — the app's existing period model), or
#   * the seeded demo-billing state (no payment rows yet, plan_status
#     "active (demo billing)") inside the same window from plan_since.
# A legacy "payment_pending" state (written by the removed Pay Later
# button) is UNPAID. A paid period that has ended is "expired": the gate
# closes again and the payment step becomes a renewal.
# ---------------------------------------------------------------------------
PERIOD_DAYS = {"monthly": 30, "yearly": 365}


def _period_end(start: date, period: str) -> date:
    return start + timedelta(days=PERIOD_DAYS.get(period, 30))


def _subscription_info_base(db: Session) -> dict:
    """The install-wide subscription state from payment rows + settings
    (no per-account override — see subscription_info)."""
    today = date.today()
    latest = (
        db.query(models.SubscriptionPayment)
        .order_by(models.SubscriptionPayment.id.desc())
        .first()
    )
    if latest is not None:
        if latest.status != "paid":
            # Legacy Pay Later row: the plan was saved but never paid for.
            return {"active": False, "status": "payment_pending", "expires_on": None}
        start = latest.created_at.date() if latest.created_at else today
        end = _period_end(start, latest.period)
        if today < end:
            return {"active": True, "status": "active", "expires_on": end.isoformat()}
        return {"active": False, "status": "expired", "expires_on": end.isoformat()}
    if (
        get_setting(db, "payment_status") == "payment_pending"
        or get_setting(db, "plan_status") == "payment_pending"
    ):
        return {"active": False, "status": "payment_pending", "expires_on": None}
    if get_setting(db, "plan_status", "").startswith("active"):
        # Seeded demo billing / legacy installs: no payment rows yet.
        since_raw = get_setting(db, "plan_since")
        try:
            since = date.fromisoformat(since_raw) if since_raw else None
        except ValueError:
            since = None
        if since is None:
            return {"active": True, "status": "active", "expires_on": None}
        end = _period_end(since, get_setting(db, "plan_period", "monthly"))
        if today < end:
            return {"active": True, "status": "active", "expires_on": end.isoformat()}
        return {"active": False, "status": "expired", "expires_on": end.isoformat()}
    return {"active": False, "status": "unpaid", "expires_on": None}


# ---------------------------------------------------------------------------
# Switch-off override (Farhan, via Control, 2026-10-23): a per-account
# revoked flag stored in settings. While it is set, the account counts
# as NOT active immediately — even inside a paid window — and the gate
# locks it. Switching off deletes NOTHING (members, check-ins and the
# payment record all stay); clearing the flag (admin Activate) restores
# access exactly as it was.
# ---------------------------------------------------------------------------
def _revoked_key(user_id: int) -> str:
    return f"revoked_user_{user_id}"


def is_revoked(db: Session, user_id: int) -> bool:
    return get_setting(db, _revoked_key(user_id)) == "1"


def set_revoked(db: Session, user_id: int, off: bool) -> None:
    set_setting(db, _revoked_key(user_id), "1" if off else "")


def _user_subscription_state(db: Session, user_id: int) -> dict:
    """One account's subscription from ITS OWN latest payment only.

    Cross-account lock (security pass, 2026-10-23): an account is
    active only if IT paid (or Farhan activated it). Another
    account's payment on the same install never unlocks this one —
    before this, the install-wide state let any second account ride
    on the first account's payment, bypassing the pay-first gate."""
    today = date.today()
    latest = _latest_payment_for(db, user_id)
    if latest is None:
        return {"active": False, "status": "unpaid", "expires_on": None}
    if latest.status != "paid":
        # Legacy row: the plan was saved but never paid for.
        return {"active": False, "status": "payment_pending", "expires_on": None}
    start = latest.created_at.date() if latest.created_at else today
    end = _period_end(start, latest.period)
    if today < end:
        return {"active": True, "status": "active", "expires_on": end.isoformat()}
    return {"active": False, "status": "expired", "expires_on": end.isoformat()}


def subscription_info(db: Session, user_id: int | None = None) -> dict:
    """{"active": bool, "status": active|payment_pending|expired|unpaid|
    switched_off, "expires_on": ISO date | None}.

    With a user_id the state is strictly THAT account's (its own
    latest payment + its switch-off override). Without one, the
    legacy install-wide state (used only by internal callers)."""
    if user_id is None:
        return _subscription_info_base(db)
    info = _user_subscription_state(db, user_id)
    if is_revoked(db, user_id):
        return {
            "active": False,
            "status": "switched_off",
            "expires_on": info["expires_on"],
        }
    return info


def get_plan_state(db: Session, user_id: int | None = None) -> dict:
    """The gym's current subscription (demo owner starts on Premium, monthly).

    Price and payment fields are computed here, server-side, from
    billing.py and the recorded subscription payments — the client never
    supplies an amount. With a user_id, that account's switch-off
    override is reflected in the subscription_* fields.
    """
    plan_id = get_setting(db, "plan_id", "pro")
    plan = get_subscription_plan(plan_id) or get_subscription_plan("pro")
    period = get_setting(db, "plan_period", "monthly")
    members_used = db.query(models.Member).count()
    recent_q = db.query(models.SubscriptionPayment)
    if user_id is not None:
        # Cross-account lock: an account sees ONLY its own payments
        # (plus legacy install-level rows whose user_id is NULL, from
        # before payments were tied to accounts). Another account's
        # payment — its amount, method or date — never leaks here.
        recent_q = recent_q.filter(
            (models.SubscriptionPayment.user_id == user_id)
            | (models.SubscriptionPayment.user_id.is_(None))
        )
    recent = recent_q.order_by(models.SubscriptionPayment.id.desc()).limit(5).all()
    last_payment = payment_dict(recent[0]) if recent else None
    payment_status = get_setting(db, "payment_status", "")
    info = subscription_info(db, user_id=user_id)
    return {
        "plan_id": plan["id"],
        "period": period,
        "status": get_setting(db, "plan_status", "active (demo billing)"),
        "since": get_setting(db, "plan_since", ""),
        "plan": plan,
        "price": plan["yearly"] if period == "yearly" else plan["monthly"],
        "payment_status": payment_status or None,  # active | payment_pending | None
        "subscription_active": info["active"],
        "subscription_status": info["status"],  # active | payment_pending | expired | unpaid | switched_off
        "subscription_expires_on": info["expires_on"],
        # The locked payment screen shows its "Unlock by Face Scan"
        # action only when Farhan has enrolled his face in Control.
        "face_unlock_available": _face_template(db) is not None,
        "last_payment": last_payment,
        "payments": [payment_dict(p) for p in recent],
        "usage": {
            "members_used": members_used,
            "member_limit": plan["member_limit"],
        },
    }


def renewed_this_month(db: Session, today: date) -> int:
    total = 0
    for r in db.query(models.Renewal).all():
        if r.date.year == today.year and r.date.month == today.month:
            total += r.amount
    return total


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
def _apply_session_cookie(resp: Response, request: Request, user: models.User) -> Response:
    """Set the signed, HTTP-only session cookie on any response type."""
    with SessionLocal() as _db:
        token = create_session_token(
            user.id, user.email, _session_epoch(_db, user.id)
        )
    # Secure cookie: ON automatically when the request arrived over HTTPS
    # (directly or via a proxy's X-Forwarded-Proto), or when forced by env.
    behind_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        or os.environ.get("COOKIE_SECURE", "") == "1"
    )
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax", path="/",
        secure=behind_https,
    )
    return resp


def _session_response(request: Request, user: models.User, extra: dict | None = None) -> JSONResponse:
    """JSON response that also sets the signed, HTTP-only session cookie."""
    resp = JSONResponse(content={"ok": True, **user_payload(user), **(extra or {})})
    return _apply_session_cookie(resp, request, user)


def _session_redirect(request: Request, user: models.User) -> RedirectResponse:
    """Browser redirect to the app that also logs the user in (OAuth return)."""
    resp = RedirectResponse(url="/", status_code=302)
    return _apply_session_cookie(resp, request, user)


@app.post("/api/signup")
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited("signup:" + ip):
        return err(429, "Too many sign-up attempts. Please wait a minute and try again.")
    fields: dict[str, str] = {}
    owner_name = body.owner_name.strip()
    gym_name = body.gym_name.strip()
    email = body.email.strip().lower()
    mobile = clean_phone(body.mobile)
    if len(owner_name) < 2:
        fields["owner_name"] = "Please enter your full name."
    if len(gym_name) < 2:
        fields["gym_name"] = "Please enter your gym's name."
    if not EMAIL_RE.match(email):
        fields["email"] = "Please enter a correct email address."
    if len(mobile) != 10:
        fields["mobile"] = "Please enter a 10-digit mobile number."
    if len(body.password) < 6:
        fields["password"] = "Please choose a password of at least 6 characters."
    if fields:
        return field_error(fields, "Please check the highlighted fields.")
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        return field_error(
            {"email": "An account with this email already exists. Please log in instead."},
            "An account with this email already exists. Please log in instead.",
        )
    user = models.User(
        email=email,
        password_hash=hash_password(body.password),
        owner_name=owner_name,
        gym_name=gym_name,
        mobile=mobile,
        auth_provider="email",
        details_completed=False,
        plan_chosen=False,
        otp_pending=True,  # canonical flow: OTP stage comes next
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("New gym signed up: %s (%s)", email, gym_name)
    code = _issue_otp(db, user)
    extra = {"otp_pending": True}
    if _dev_otp_echo():
        extra["dev_otp"] = code
    return _session_response(request, user, extra)
@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited(ip):
        return err(429, "Too many login attempts. Please wait a minute and try again.")
    user = db.query(models.User).filter(models.User.email == body.email.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        return err(401, "Wrong email or password. Please try again.")
    logger.info("Login success for %s from %s", user.email, ip)
    extra = None
    if getattr(user, "otp_pending", False):
        # Still in the OTP stage: make sure a live code exists (a
        # fresh one is sent when the old one expired or was used up).
        extra = {"otp_pending": True}
        if _otp_live(db, user) is None:
            code = _issue_otp(db, user)
            if _dev_otp_echo():
                extra["dev_otp"] = code
        elif _dev_otp_echo():
            row = _otp_challenge(db, user)
            if row is not None and row.dev_code:
                extra["dev_otp"] = row.dev_code
    return _session_response(request, user, extra)


@app.post("/api/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    # Bump the account's session epoch: every token issued so far —
    # including one an attacker might have copied — stops working at
    # once, not just this browser's cookie.
    payload = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if payload and payload.get("uid"):
        user = db.query(models.User).filter(
            models.User.id == int(payload["uid"])
        ).first()
        if user is not None:
            user.session_epoch = int(getattr(user, "session_epoch", 0) or 0) + 1
            db.commit()
            logger.info("Logout: session epoch bumped for user %s", user.id)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------------------
# Google sign-in (OAuth 2.0 / OpenID Connect) — config in google_auth.py.
# The browser flow is: /auth/google -> Google consent -> /auth/google/callback
# Every failure lands back on the login page with a friendly message in
# ?auth_error=... — never a raw error page.
# ---------------------------------------------------------------------------
def _auth_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(url="/?auth_error=" + quote(message), status_code=302)


@app.get("/api/auth/config")
def auth_config():
    """Public: which sign-in options the frontend should offer, and
    whether outgoing mail is configured (a boolean only — never any
    value or fragment of the SMTP settings) so a missing mail setup is
    visible from outside instead of failing silently."""
    return {
        "google_enabled": google_auth_enabled(),
        "mail_configured": _smtp_config() is not None,
    }


@app.get("/auth/google")
def google_start(request: Request):
    """Start Google sign-in: send the browser to Google's consent screen."""
    if not google_auth_enabled():
        return _auth_error_redirect(
            "Google sign-in is not switched on for this app yet. "
            "Please log in with your email and password."
        )
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited("google:" + ip):
        return _auth_error_redirect(
            "Too many sign-in attempts. Please wait a minute and try again."
        )
    return RedirectResponse(url=build_authorize_url(create_oauth_state()), status_code=302)


@app.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    """Google redirects the browser here after the consent screen."""
    if not google_auth_enabled():
        return _auth_error_redirect(
            "Google sign-in is not switched on for this app yet. "
            "Please log in with your email and password."
        )
    if error or not code:
        logger.info("Google sign-in cancelled or denied (error=%s)", error or "no code")
        return _auth_error_redirect(
            "Google sign-in was cancelled. Please try again, "
            "or log in with your email and password."
        )
    if not verify_oauth_state(state):
        logger.warning("Google sign-in arrived with an invalid or expired state value")
        return _auth_error_redirect(
            "That Google sign-in attempt has expired. Please try signing in with Google again."
        )
    try:
        access_token = exchange_code_for_token(code)
        info = fetch_google_userinfo(access_token)
    except Exception:
        logger.exception("Google sign-in failed while talking to Google")
        return _auth_error_redirect(
            "We could not complete Google sign-in just now. Please try again in a moment."
        )
    email = (info.get("email") or "").strip().lower()
    if not info.get("email_verified") or not EMAIL_RE.match(email):
        logger.warning("Google sign-in refused: no verified email from Google")
        return _auth_error_redirect(
            "Google did not share a verified email address, so we cannot sign you in. "
            "Please log in with your email and password instead."
        )
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        # First Google sign-in: create the account. There is no password —
        # Google verifies identity — so a random, unknown hash fills the
        # (non-null) column and password login simply never matches.
        # Google's verified email IS the verification (his order,
        # superseding the earlier Google -> OTP -> details flow): the
        # account is born verified, no OTP is issued, and the entry flow
        # goes straight to collecting gym & owner details.
        user = models.User(
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            owner_name=(info.get("name") or "").strip()[:120],
            auth_provider="google",
            details_completed=False,
            plan_chosen=False,
            otp_pending=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("New gym signed up via Google: %s", email)
    else:
        logger.info("Login via Google for %s", email)
        if getattr(user, "otp_pending", False):
            # Google just proved ownership of this inbox (an unverified
            # email was refused above), so a pending OTP stage — e.g.
            # from an earlier email signup whose code never arrived —
            # is cleared on the spot: the exact end-state of a
            # successful OTP entry, challenge row deleted.
            row = _otp_challenge(db, user)
            if row is not None:
                db.delete(row)
            user.otp_pending = False
            db.commit()
            logger.info(
                "Google sign-in verified %s — pending OTP stage cleared", email
            )
    return _session_redirect(request, user)


# ---------------------------------------------------------------------------
# Email OTP — stage 2 of the email/password signup flow: (1) account
# created via email/password, (2) a 6-digit OTP is generated and sent
# to that email and the owner enters it here, (3) only then Gym &
# Owner Details, (4) payment, (5) the animated unlock. The Google
# route no longer passes through this stage (owner's new order,
# superseding the earlier Google -> OTP -> details flow): Google
# sign-in verifies by itself — see google_callback above.
#
# Delivery: real email via SMTP when SMTP_HOST / SMTP_PORT /
# SMTP_USER / SMTP_PASS / SMTP_FROM are set (a Gmail App Password
# works — see the README publish checklist). Until then (pre-launch)
# the code is written to the server log, and ONLY when the explicit
# dev flag DEV_OTP_ECHO=1 is set does an API response carry it — that
# flag exists for local dev/tests and must stay OFF in production.
# Codes are stored HASHED, live 10 minutes, allow 5 attempts, and a
# resend waits out a 60-second cooldown.
# ---------------------------------------------------------------------------
OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = 60  # seconds


def _dev_otp_echo() -> bool:
    return os.environ.get("DEV_OTP_ECHO", "").strip() == "1"


def _smtp_config() -> dict | None:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not (host and user and password and sender):
        return None
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return {"host": host, "port": port, "user": user, "pass": password, "from": sender}


def _otp_hash(user: models.User, code: str) -> str:
    raw = f"{user.id}:{user.email.lower()}:{code}".encode()
    return hashlib.sha256(raw).hexdigest()


def _deliver_code(email: str, code: str, subject: str, body: str, label: str) -> None:
    """The one SMTP path every emailed code travels (signup OTP and
    password-reset code alike). Never raises — a delivery hiccup must
    not break the flow that issued the code. When SMTP is not
    configured the code is logged ONLY under the dev-only DEV_OTP_ECHO
    flag; in production it is never written anywhere."""
    cfg = _smtp_config()
    if cfg is None:
        if _dev_otp_echo():
            # Dev only: the echo flag exists so local dev/tests can
            # complete the flow without an SMTP server.
            logger.info(
                "%s for %s: %s (DEV_OTP_ECHO on — code logged for dev only)",
                label, email, code,
            )
        else:
            # Production without SMTP: the code is NEVER written to a
            # log or a response. (Publish step: set SMTP_* — see README.)
            logger.info(
                "%s issued for %s (SMTP not configured — code not logged; "
                "set SMTP_* to email codes for real)", label, email,
            )
        return
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = cfg["from"]
        msg["To"] = email
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.starttls()
            server.login(cfg["user"], cfg["pass"])
            server.send_message(msg)
        logger.info("%s email sent to %s via SMTP", label, email)
    except Exception:
        logger.exception("%s email to %s FAILED via SMTP", label, email)


def _deliver_otp(email: str, code: str) -> None:
    """Send the signup OTP email (see _deliver_code for the shared
    SMTP path and the no-SMTP logging rules)."""
    _deliver_code(
        email, code,
        "Your Do Gym OTP",
        f"Your Do Gym one-time passcode is {code}.\n\n"
        "It works for 10 minutes. If you did not ask for it, "
        "you can ignore this email.\n",
        "OTP",
    )


def _deliver_password_reset(email: str, code: str) -> None:
    """Send the password-reset code email — same SMTP path as the
    signup OTP, its own subject and wording."""
    _deliver_code(
        email, code,
        "Reset your Do Gym password",
        f"Your Do Gym password-reset code is {code}.\n\n"
        "It works for 10 minutes. Enter it on the Forgot Password "
        "screen to set a new password.\n\n"
        "If you did not ask for it, you can ignore this email — "
        "your password stays unchanged.\n",
        "Password-reset code",
    )


def _otp_challenge(db: Session, user: models.User):
    return (
        db.query(models.OtpChallenge)
        .filter(models.OtpChallenge.user_id == user.id)
        .first()
    )


def _otp_live(db: Session, user: models.User):
    """The account's challenge row if it is still usable, else None."""
    row = _otp_challenge(db, user)
    if row is None or row.expires_at <= datetime.now():
        return None
    return row


def _issue_otp(db: Session, user: models.User) -> str:
    """Generate a fresh OTP (replacing any old one), deliver it, and
    return the plain code (used only for the dev echo)."""
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    now = datetime.now()
    row = _otp_challenge(db, user)
    if row is None:
        row = models.OtpChallenge(user_id=user.id)
        db.add(row)
    row.code_hash = _otp_hash(user, code)
    row.dev_code = code if _dev_otp_echo() else None
    row.attempts = 0
    row.last_sent_at = now
    row.expires_at = now + OTP_TTL
    db.commit()
    _deliver_otp(user.email, code)
    return code


def _otp_state(db: Session, user: models.User) -> dict:
    """OTP timing fields for /api/me — drives the OTP screen's resend
    countdown and code-expiry note."""
    state = {"otp_resend_in": 0, "otp_expires_in": None}
    if not getattr(user, "otp_pending", False):
        return state
    row = _otp_challenge(db, user)
    if row is None:
        return state
    now = datetime.now()
    state["otp_resend_in"] = max(
        0, OTP_RESEND_COOLDOWN - int((now - row.last_sent_at).total_seconds())
    )
    state["otp_expires_in"] = max(0, int((row.expires_at - now).total_seconds()))
    return state


def _otp_err(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"detail": message, "code": code}
    )


@app.post("/api/auth/otp/send")
def otp_send(
    db: Session = Depends(get_db),
    user: models.User | JSONResponse = Depends(current_user),
):
    """(Re)send the OTP. A resend waits out the 60-second cooldown;
    with DEV_OTP_ECHO=1 the response carries the live code (dev/tests
    only — off in production)."""
    if isinstance(user, JSONResponse):
        return user
    if not getattr(user, "otp_pending", False):
        return {"ok": True, "otp_pending": False}
    row = _otp_challenge(db, user)
    if row is not None:
        waited = (datetime.now() - row.last_sent_at).total_seconds()
        if waited < OTP_RESEND_COOLDOWN:
            if _dev_otp_echo() and row.dev_code:
                # Dev/test only: hand back the still-live code instead
                # of making the caller wait out the cooldown.
                return {"ok": True, "otp_pending": True, "dev_otp": row.dev_code}
            return _otp_err(
                429,
                f"Please wait {int(OTP_RESEND_COOLDOWN - waited)} seconds "
                "before asking for a new OTP.",
                "otp_cooldown",
            )
    code = _issue_otp(db, user)
    out = {"ok": True, "otp_pending": True}
    if _dev_otp_echo():
        out["dev_otp"] = code
    return out


class OtpVerifyIn(BaseModel):
    code: str = Field(min_length=4, max_length=8)


@app.post("/api/auth/otp/verify")
def otp_verify(
    body: OtpVerifyIn,
    db: Session = Depends(get_db),
    user: models.User | JSONResponse = Depends(current_user),
):
    """Enter the OTP. The right code ends the OTP stage and the
    normal flow (details -> payment) opens. A wrong code burns one of
    5 attempts; an expired code must be resent."""
    if isinstance(user, JSONResponse):
        return user
    if not getattr(user, "otp_pending", False):
        return {"ok": True, "otp_pending": False, "already_verified": True}
    row = _otp_challenge(db, user)
    if row is None or row.expires_at <= datetime.now():
        return _otp_err(
            400,
            "That OTP has expired. Please tap Resend for a fresh one.",
            "otp_expired",
        )
    if row.attempts >= OTP_MAX_ATTEMPTS:
        return _otp_err(
            400,
            "Too many wrong attempts. Please tap Resend for a fresh OTP.",
            "otp_locked",
        )
    if not hmac.compare_digest(_otp_hash(user, body.code.strip()), row.code_hash):
        row.attempts += 1
        db.commit()
        left = OTP_MAX_ATTEMPTS - row.attempts
        logger.info("OTP verify failed for %s (%d attempt(s) left)", user.email, left)
        if left <= 0:
            return _otp_err(
                400,
                "Too many wrong attempts. Please tap Resend for a fresh OTP.",
                "otp_locked",
            )
        return _otp_err(
            400, f"That OTP is not correct. {left} attempt(s) left.", "otp_wrong"
        )
    user.otp_pending = False
    db.delete(row)
    db.commit()
    logger.info("OTP verified for %s — details/payment stage unlocked", user.email)
    return {"ok": True, **user_payload(user)}


# ---------------------------------------------------------------------------
# Password reset — "Forgot password?" on the login screen.
#
# POST /api/auth/forgot {email}: if the account exists, a 6-digit
# reset code is generated and emailed through the SAME SMTP path as
# the signup OTP. The response is ALWAYS the same generic success —
# whether the account exists or not, and whether a rate limit or the
# resend cooldown quietly swallowed the send — so this endpoint can
# never be used to probe which emails have accounts.
#
# POST /api/auth/reset {email, code, new_password}: the code is
# checked with the signup OTP's rules (hashed, 10 minutes, 5
# attempts) and, on success, the password hash is replaced, the code
# is consumed, and the account's session epoch is bumped — every
# existing session dies at once, exactly like logout. The email is
# NOT verified by a reset: an account still in the signup-OTP stage
# stays otp_pending and lands back on that stage at next login.
#
# Purpose separation is structural: reset codes live in their own
# table (PasswordResetChallenge) and are hashed with a "pwreset:"
# domain prefix, so a signup OTP can never reset a password and a
# reset code can never verify an email — even if the digits matched.
# ---------------------------------------------------------------------------
RESET_GENERIC_MESSAGE = (
    "If an account with this email exists, a reset code has been sent."
)


def _reset_hash(user: models.User, code: str) -> str:
    raw = f"pwreset:{user.id}:{user.email.lower()}:{code}".encode()
    return hashlib.sha256(raw).hexdigest()


def _reset_challenge(db: Session, user: models.User):
    return (
        db.query(models.PasswordResetChallenge)
        .filter(models.PasswordResetChallenge.user_id == user.id)
        .first()
    )


def _issue_password_reset(db: Session, user: models.User) -> str:
    """Generate a fresh reset code (replacing any old one), deliver
    it, and return the plain code (used only by tests via dev_code)."""
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    now = datetime.now()
    row = _reset_challenge(db, user)
    if row is None:
        row = models.PasswordResetChallenge(user_id=user.id)
        db.add(row)
    row.code_hash = _reset_hash(user, code)
    row.dev_code = code if _dev_otp_echo() else None
    row.attempts = 0
    row.last_sent_at = now
    row.expires_at = now + OTP_TTL
    db.commit()
    _deliver_password_reset(user.email, code)
    return code


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


@app.post("/api/auth/forgot")
def forgot_password(body: ForgotIn, request: Request, db: Session = Depends(get_db)):
    """Ask for a password-reset code. ALWAYS answers the same
    generic success (see the section comment): the rate limits and
    the 60-second resend cooldown throttle the SENDING, never the
    answer, so nothing about the account leaks."""
    ip = request.client.host if request.client else "unknown"
    email = body.email.strip().lower()
    generic = {"ok": True, "message": RESET_GENERIC_MESSAGE}
    if login_rate_limited("forgot:" + ip):
        logger.info("Forgot-password send throttled (per-IP limit)")
        return generic
    if login_rate_limited("forgot-email:" + email):
        logger.info("Forgot-password send throttled (per-email limit)")
        return generic
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        return generic
    row = _reset_challenge(db, user)
    if row is not None:
        waited = (datetime.now() - row.last_sent_at).total_seconds()
        if waited < OTP_RESEND_COOLDOWN:
            # A fresh code is already in the inbox — keep it valid
            # instead of burning it with a re-issue.
            return generic
    _issue_password_reset(db, user)
    return generic


class ResetIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=1, max_length=200)


def _reset_invalid() -> JSONResponse:
    """The ONE reset failure answer — wrong code, expired code,
    spent code, unknown account: indistinguishable from outside."""
    return _otp_err(
        400,
        "That code is not correct or has expired. "
        "Please check it, or ask for a new one.",
        "reset_invalid",
    )


@app.post("/api/auth/reset")
def reset_password(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    """Set a new password with a reset code. On success every
    existing session for the account is invalidated (session epoch
    bump, the logout mechanism); the account's otp_pending state is
    deliberately left untouched."""
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited("reset:" + ip):
        return err(429, "Too many attempts. Please wait a minute and try again.")
    if len(body.new_password) < 6:
        return field_error(
            {"new_password": "Please choose a password of at least 6 characters."},
            "Please check the highlighted fields.",
        )
    email = body.email.strip().lower()
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        return _reset_invalid()
    row = _reset_challenge(db, user)
    if row is None or row.expires_at <= datetime.now():
        return _reset_invalid()
    if row.attempts >= OTP_MAX_ATTEMPTS:
        return _reset_invalid()
    if not hmac.compare_digest(_reset_hash(user, body.code.strip()), row.code_hash):
        row.attempts += 1
        db.commit()
        logger.info(
            "Password-reset verify failed for %s (%d attempt(s) left)",
            user.email, OTP_MAX_ATTEMPTS - row.attempts,
        )
        return _reset_invalid()
    user.password_hash = hash_password(body.new_password)
    # Invalidate EVERY existing session for this account at once —
    # a password reset must kick out anyone holding an old cookie
    # (the very scenario a reset usually answers).
    user.session_epoch = int(getattr(user, "session_epoch", 0) or 0) + 1
    db.delete(row)
    db.commit()
    logger.info("Password reset completed for %s — all sessions invalidated", user.email)
    return {"ok": True}


@app.get("/api/me")
def me(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    return {**user_payload(user), **_otp_state(db, user)}


@app.get("/api/details")
def get_details(user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    return details_payload(user)


@app.put("/api/details")
def save_details(body: DetailsIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    fields: dict[str, str] = {}
    gym_phone = clean_phone(body.gym_phone)
    mobile = clean_phone(body.mobile)
    whatsapp = clean_phone(body.whatsapp) if body.whatsapp.strip() else ""
    if len(body.gym_name.strip()) < 2:
        fields["gym_name"] = "Please enter your gym's name."
    if len(body.gym_address.strip()) < 5:
        fields["gym_address"] = "Please enter your gym's full address."
    if len(body.gym_city.strip()) < 2:
        fields["gym_city"] = "Please enter your city."
    if len(gym_phone) != 10:
        fields["gym_phone"] = "Please enter a 10-digit phone number for the gym."
    if body.opening_year is not None and not (1950 <= body.opening_year <= date.today().year):
        fields["opening_year"] = "Please enter a correct year (for example 2019)."
    if len(body.owner_name.strip()) < 2:
        fields["owner_name"] = "Please enter your full name."
    if len(mobile) != 10:
        fields["mobile"] = "Please enter your 10-digit mobile number."
    if whatsapp and len(whatsapp) != 10:
        fields["whatsapp"] = "Please enter a 10-digit WhatsApp number, or leave it empty."
    if fields:
        return field_error(fields, "Please check the highlighted fields.")
    user.gym_name = body.gym_name.strip()
    user.gym_address = body.gym_address.strip()
    user.gym_city = body.gym_city.strip()
    user.gym_phone = gym_phone
    user.opening_year = body.opening_year
    user.owner_name = body.owner_name.strip()
    user.mobile = mobile
    user.whatsapp = whatsapp
    user.details_completed = True
    db.commit()
    logger.info("Gym & owner details saved for %s", user.email)
    return {"ok": True, **details_payload(user), **user_payload(user)}


# ---------------------------------------------------------------------------
# App endpoints (all require login)
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    members = db.query(models.Member).all()
    due = [m for m in members if 0 <= (m.expiry_date - today).days <= 7]
    expired = [m for m in members if (m.expiry_date - today).days < 0]
    today_checkins = db.query(models.CheckIn).filter(models.CheckIn.date == today).all()
    return {
        "total_members": len(members),
        "checked_in_today": len({c.member_id for c in today_checkins}),
        "red_list_count": len(red_list(db, today)),
        "renewals_due": len(due),
        "expired_count": len(expired),
        "renewed_this_month": renewed_this_month(db, today),
        "mode": get_mode(db),
    }


@app.get("/api/members")
def members(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    return {"members": [member_dict(db, m, today) for m in db.query(models.Member).order_by(models.Member.id).all()]}


@app.get("/api/red-list")
def red_list_endpoint(min_days: int = 10, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    if min_days not in (10, 15, 20):
        return err(400, "min_days must be 10, 15 or 20.")
    return {"members": red_list(db, date.today(), min_days=min_days), "min_days": min_days}


@app.post("/api/red-list/flag")
def flag_member(body: MemberRefIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    m = db.query(models.Member).filter(models.Member.id == body.member_id).first()
    if not m:
        return err(404, "Member not found.")
    if not db.query(models.ManualFlag).filter(models.ManualFlag.member_id == m.id).first():
        db.add(models.ManualFlag(member_id=m.id))
    clear = db.query(models.ManualClear).filter(models.ManualClear.member_id == m.id).first()
    if clear:
        db.delete(clear)
    db.commit()
    return {"ok": True}


@app.delete("/api/red-list/flag/{member_id}")
def unflag_member(member_id: int, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    row = db.query(models.ManualFlag).filter(models.ManualFlag.member_id == member_id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@app.post("/api/red-list/clear")
def clear_member(body: MemberRefIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    m = db.query(models.Member).filter(models.Member.id == body.member_id).first()
    if not m:
        return err(404, "Member not found.")
    if not db.query(models.ManualClear).filter(models.ManualClear.member_id == m.id).first():
        db.add(models.ManualClear(member_id=m.id))
    flag = db.query(models.ManualFlag).filter(models.ManualFlag.member_id == m.id).first()
    if flag:
        db.delete(flag)
    db.commit()
    return {"ok": True}


@app.post("/api/checkin")
def checkin(body: CheckInIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    member = None
    if body.member_id is not None:
        member = db.query(models.Member).filter(models.Member.id == body.member_id).first()
    elif body.code:
        member = db.query(models.Member).filter(models.Member.code == body.code.strip().upper()).first()
    if not member:
        return err(404, "Member not found. Check the code and try again.")
    today = date.today()
    if checked_in_today(db, member.id, today):
        return err(400, f"{member.name} is already checked in today.")
    now_str = datetime.now().strftime("%I:%M %p").lstrip("0")
    db.add(models.CheckIn(member_id=member.id, date=today, time_str=now_str, source="owner"))
    db.commit()
    return {
        "ok": True,
        "member": member_dict(db, member, today),
        "red_list_count": len(red_list(db, today)),
        "message": f"{member.name} checked in successfully.",
    }


@app.delete("/api/checkin/today/{member_id}")
def unmark_today(member_id: int, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Manual-mode undo: remove today's check-in for a member."""
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    rows = db.query(models.CheckIn).filter(
        models.CheckIn.member_id == member_id, models.CheckIn.date == today
    ).all()
    for r in rows:
        db.delete(r)
    db.commit()
    return {"ok": True}


@app.get("/api/checkins/today")
def checkins_today(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    rows = db.query(models.CheckIn).filter(models.CheckIn.date == today).order_by(models.CheckIn.id.desc()).all()
    out = []
    for c in rows:
        m = db.query(models.Member).filter(models.Member.id == c.member_id).first()
        if m:
            out.append({
                "member_id": m.id, "name": m.name, "code": m.code,
                "time": c.time_str, "source": c.source or "owner",
            })
    return {"checkins": out}


@app.get("/api/renewals")
def renewals(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    due, expired = [], []
    for m in db.query(models.Member).all():
        d = member_dict(db, m, today)
        d["amount"] = PLANS[m.plan]["price"]
        if d["days_left"] < 0:
            expired.append(d)
        elif d["days_left"] <= 7:
            due.append(d)
    due.sort(key=lambda r: r["days_left"])
    expired.sort(key=lambda r: r["days_left"])
    history = [
        {
            "member_name": r.member_name, "plan": r.plan, "amount": r.amount,
            "date": r.date.isoformat(), "new_expiry": r.new_expiry.isoformat(),
        }
        for r in db.query(models.Renewal).order_by(models.Renewal.date.desc(), models.Renewal.id.desc()).all()
    ]
    return {
        "due": due,
        "expired": expired,
        "history": history,
        "stats": {
            "due_count": len(due),
            "expired_count": len(expired),
            "renewed_this_month": renewed_this_month(db, today),
        },
    }


@app.post("/api/renewals/{member_id}/renew")
def renew(member_id: int, body: RenewIn | None = None, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    m = db.query(models.Member).filter(models.Member.id == member_id).first()
    if not m:
        return err(404, "Member not found.")
    today = date.today()
    days = body.custom_days if body and body.custom_days else PLANS[m.plan]["days"]
    m.expiry_date = today + timedelta(days=days)
    amount = PLANS[m.plan]["price"]
    db.add(models.Renewal(
        member_id=m.id, member_name=m.name, plan=m.plan, amount=amount,
        date=today, new_expiry=m.expiry_date,
    ))
    db.commit()
    return {
        "ok": True,
        "member": member_dict(db, m, today),
        "amount": amount,
        "message": f"{m.name} renewed ({m.plan}) until {m.expiry_date.isoformat()}.",
    }


@app.get("/api/search")
def search(q: str = "", db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Member search by name or member code (case-insensitive, partial)."""
    if isinstance(user, JSONResponse):
        return user
    today = date.today()
    needle = q.strip().lower()
    members = db.query(models.Member).order_by(models.Member.id).all()
    if needle:
        members = [
            m for m in members
            if needle in m.name.lower() or needle in m.code.lower()
        ]
    return {"members": [member_dict(db, m, today) for m in members], "query": q}


@app.get("/api/mode")
def get_mode_endpoint(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    return {"mode": get_mode(db)}


@app.put("/api/mode")
def set_mode(body: ModeIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    row = db.query(models.Setting).filter(models.Setting.key == "mode").first()
    if not row:
        row = models.Setting(key="mode", value=body.mode)
        db.add(row)
    else:
        row.value = body.mode
    db.commit()
    return {"mode": body.mode}


@app.get("/api/plans")
def list_plans():
    """Public: the 2 subscription plans (single source = billing.py)."""
    return {"plans": SUBSCRIPTION_PLANS}


@app.get("/api/plan")
def current_plan(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    return get_plan_state(db, user_id=user.id)


@app.post("/api/plan/switch")
def switch_plan(body: PlanSwitchIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Record the owner's plan CHOICE (entry flow / payment step).

    Choosing is not paying: this never activates a subscription. The
    pay-first gate lifts only when a payment is recorded — by Farhan's
    admin activation after an in-person collection (the launch model),
    or by Razorpay's webhook if the gateway is ever switched on.
    """
    if isinstance(user, JSONResponse):
        return user
    plan = get_subscription_plan(body.plan_id)
    if not plan:
        return err(404, "Plan not found.")
    set_setting(db, "plan_id", body.plan_id)
    set_setting(db, "plan_period", body.period)
    db.commit()
    # Audit: every plan change is written to the server log.
    logger.info(
        "Plan chosen by %s -> %s (%s); subscription state unchanged "
        "(payment still required)",
        user.email, body.plan_id, body.period,
    )
    user.plan_chosen = True
    db.commit()
    state = get_plan_state(db, user_id=user.id)
    state["message"] = f"You have chosen the {plan['name']} plan ({body.period})."
    return state


# ---------------------------------------------------------------------------
# Subscription payments — NO confirmation buttons anywhere, and owners
# can NEVER activate themselves (owner orders, 2026-10-23). A payment
# is recorded on the server only, through exactly two channels:
#
#   1. PRIMARY (the launch collection model): Farhan visits the gym,
#      collects the money in person, and activates the gym from his
#      Control page (/control) or the ADMIN_SECRET API — recorded with
#      method "collected_in_person" (see the platform-admin section).
#   2. Razorpay's webhook (dormant, env-gated): if online payment is
#      ever switched on, Razorpay itself confirms the money arrived.
#
# The old owner-facing confirm endpoint ("I Have Paid") is DELETED —
# letting owners self-activate would defeat in-person collection. The
# owner's app simply unlocks by itself the moment a payment lands:
# their open payment page polls /api/plan, turns green ("Payment
# Done ✓") and walks into the dashboard.
# ---------------------------------------------------------------------------
def _record_subscription_payment(
    db: Session,
    user: models.User,
    plan_id: str,
    period: str,
    method: str = "upi_qr",
    gateway_order_id: str | None = None,
    gateway_payment_id: str | None = None,
):
    """Record a CONFIRMED subscription payment and activate the plan.

    The amount ALWAYS comes from billing.py (server-side) — callers
    pass only plan + period. Writes a SubscriptionPayment row (status
    "paid"), updates the subscription settings, marks the plan as chosen
    (entry flow) and audit-logs the event. The pay-first gate lifts from
    this moment: subscription_info() reads this row as ACTIVE.
    """
    plan = get_subscription_plan(plan_id)
    if not plan:
        return None
    amount = plan["monthly"] if period == "monthly" else plan["yearly"]
    db.add(models.SubscriptionPayment(
        user_id=user.id,
        plan_id=plan["id"],
        plan_name=plan["name"],
        period=period,
        amount=amount,
        method=method,
        status="paid",
        gateway_order_id=gateway_order_id,
        gateway_payment_id=gateway_payment_id,
        created_at=datetime.now(),
    ))
    set_setting(db, "plan_id", plan["id"])
    set_setting(db, "plan_period", period)
    set_setting(db, "plan_status", "active")
    set_setting(db, "payment_status", "active")
    set_setting(db, "payment_method", method)
    set_setting(db, "payment_amount", str(amount))
    set_setting(db, "plan_since", date.today().isoformat())
    user.plan_chosen = True
    db.commit()
    logger.info(
        "Subscription payment PAID by %s -> %s (%s), amount ₹%d via %s",
        user.email, plan["id"], period, amount, method,
    )
    state = get_plan_state(db, user_id=user.id)
    state["message"] = (
        f"Payment recorded — your {plan['name']} plan ({period}) is active. "
        "Thank you!"
    )
    return state


@app.post("/api/subscription/order")
def create_subscription_order(body: SubscriptionPayIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Create a Razorpay order (DORMANT gateway plumbing — not used by
    the QR-only payment panel, and not the launch payment route).

    Kept env-gated for the optional future online-payment route: if
    Razorpay keys are ever set, this creates the order and Razorpay's
    webhook (below) is what records the payment and unlocks the app —
    confirmation never comes from the browser. While the gateway is
    not configured it answers 404.
    """
    if isinstance(user, JSONResponse):
        return user
    if not payments_razorpay.razorpay_enabled():
        return err(404, "Online payment is not switched on yet.")
    plan = get_subscription_plan(body.plan_id)
    if not plan:
        return err(404, "Plan not found.")
    amount = plan["monthly"] if body.period == "monthly" else plan["yearly"]
    receipt = f"dogym-{user.id}-{int(time.time())}"[:40]
    notes = {
        "user_id": str(user.id),
        "plan_id": plan["id"],
        "period": body.period,
        "app": "do-gym-subscription",
    }
    try:
        order = payments_razorpay.create_order(amount * 100, receipt, notes)
    except payments_razorpay.RazorpayError:
        logger.exception("Razorpay order creation failed for %s", user.email)
        return err(502, "The payment service could not be reached. Please try again in a moment.")
    order_id = order.get("id")
    if not order_id:
        logger.error("Razorpay order response had no id for %s: %s", user.email, order)
        return err(502, "The payment service gave an unexpected reply. Please try again.")
    db.add(models.GatewayOrder(
        order_id=order_id,
        user_id=user.id,
        plan_id=plan["id"],
        period=body.period,
        amount=amount,
        status="created",
        created_at=datetime.now(),
    ))
    db.commit()
    logger.info(
        "Razorpay order %s created for %s -> %s (%s), amount ₹%d",
        order_id, user.email, plan["id"], body.period, amount,
    )
    return {
        "gateway": "razorpay",
        "order_id": order_id,
        "key_id": payments_razorpay.razorpay_key_id(),
        "amount": amount,               # ₹, server-computed from billing.py
        "amount_paise": amount * 100,   # what Checkout expects
        "currency": "INR",
        "plan_id": plan["id"],
        "plan_name": plan["name"],
        "period": body.period,
    }


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay webhook — THE confirmation channel (no session, public).

    Razorpay calls this when money actually moves. The call is trusted
    only after its HMAC-SHA256 signature (over the exact raw body, with
    RAZORPAY_WEBHOOK_SECRET) verifies. On payment.captured / order.paid
    for an order we created, the payment is recorded through the same
    path as any confirmed payment, so the pay-first gate lifts by
    itself. Deliveries are idempotent: Razorpay may send the same event
    more than once — the same payment id is never recorded twice.
    """
    if not payments_razorpay.razorpay_webhook_enabled():
        return err(404, "Not found.")
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not payments_razorpay.verify_webhook_signature(raw_body, signature):
        logger.warning("Razorpay webhook REJECTED: bad signature (%d bytes)", len(raw_body))
        return err(400, "Invalid signature.")
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return err(400, "Invalid payload.")
    event_name = event.get("event", "")
    if event_name not in ("payment.captured", "order.paid"):
        return {"status": "ignored", "event": event_name}
    payload = event.get("payload") or {}
    payment_entity = (payload.get("payment") or {}).get("entity") or {}
    order_entity = (payload.get("order") or {}).get("entity") or {}
    order_id = payment_entity.get("order_id") or order_entity.get("id")
    payment_id = payment_entity.get("id")
    if not order_id or not payment_id:
        logger.warning("Razorpay webhook %s missing order/payment id", event_name)
        return {"status": "ignored", "event": event_name}
    gateway_order = (
        db.query(models.GatewayOrder)
        .filter(models.GatewayOrder.order_id == order_id)
        .first()
    )
    if gateway_order is None:
        # Not an order this server created — acknowledge so Razorpay
        # stops retrying, but record nothing.
        logger.warning("Razorpay webhook for unknown order %s — ignored", order_id)
        return {"status": "ignored", "event": event_name}
    # Cross-check the user the order was created for (its notes) against
    # the row we stored at creation time, when the event carries notes.
    notes = payment_entity.get("notes") or order_entity.get("notes") or {}
    notes_user = notes.get("user_id")
    if notes_user is not None and str(notes_user) != str(gateway_order.user_id):
        logger.warning(
            "Razorpay webhook user mismatch for order %s — ignored", order_id
        )
        return {"status": "ignored", "event": event_name}
    # Cross-check the captured amount against the order's server-side
    # amount (Razorpay reports paise) when the event carries one.
    captured_paise = payment_entity.get("amount")
    if captured_paise is not None and int(captured_paise) != gateway_order.amount * 100:
        logger.warning(
            "Razorpay webhook amount mismatch for order %s — ignored", order_id
        )
        return {"status": "ignored", "event": event_name}
    # Idempotency: this Razorpay payment must be recorded exactly once.
    already = (
        db.query(models.SubscriptionPayment)
        .filter(models.SubscriptionPayment.gateway_payment_id == payment_id)
        .first()
    )
    if already is not None or gateway_order.status == "paid":
        gateway_order.payment_id = gateway_order.payment_id or payment_id
        gateway_order.status = "paid"
        db.commit()
        return {"status": "already_recorded"}
    user = db.query(models.User).filter(models.User.id == gateway_order.user_id).first()
    if user is None:
        logger.error("Razorpay webhook: user %s for order %s not found", gateway_order.user_id, order_id)
        return {"status": "ignored", "event": event_name}
    state = _record_subscription_payment(
        db, user, gateway_order.plan_id, gateway_order.period,
        method="razorpay",
        gateway_order_id=order_id,
        gateway_payment_id=payment_id,
    )
    if state is None:
        logger.error("Razorpay webhook: plan %s no longer exists", gateway_order.plan_id)
        return {"status": "ignored", "event": event_name}
    gateway_order.payment_id = payment_id
    gateway_order.status = "paid"
    db.commit()
    logger.info(
        "Razorpay webhook %s: payment %s recorded for order %s — subscription ACTIVE",
        event_name, payment_id, order_id,
    )
    return {"status": "recorded"}


# ---------------------------------------------------------------------------
# Platform admin — Farhan's control (2026-10-23). The launch collection
# model: Farhan personally visits each gym, collects the subscription
# money in person, then activates that gym from here. Owners can never
# activate themselves. He can also SWITCH a gym OFF (non-payment): the
# account locks at once, but nothing is ever deleted.
#
# Access is the ADMIN_SECRET environment variable, read on every call:
#   * unset → every admin route answers 404 (they do not exist);
#   * API (this section): header "X-Admin-Secret", compared in
#     constant time — for scripts/curl;
#   * Page (next section): GET /control — a password form (the secret
#     itself), then a signed admin cookie (12h, HTTP-only, separate
#     from owner sessions, set once per path scope: /control and
#     /api/admin). Page/API POSTs made with the cookie must also send
#     the CSRF value bound into that cookie.
# No link to /control exists anywhere in the owner UI.
# ---------------------------------------------------------------------------
def _admin_secret() -> str:
    return os.environ.get("ADMIN_SECRET", "").strip()


def _admin_enabled() -> bool:
    return bool(_admin_secret())


def _secret_matches(candidate) -> bool:
    secret = _admin_secret()
    return (
        bool(secret)
        and bool(candidate)
        and hmac.compare_digest(str(candidate), secret)
    )


def _inr(n) -> str:
    """₹ amount with Indian digit grouping (e.g. 1899 → '1,899',
    100000 → '1,00,000') for the control page."""
    s = str(int(n or 0))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _fmt_date(iso) -> str:
    """'2026-11-22' (or a datetime ISO string) → '22 Nov 2026'."""
    if not iso:
        return "—"
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d %b %Y")
    except ValueError:
        return "—"


def _latest_payment_for(db: Session, user_id: int):
    return (
        db.query(models.SubscriptionPayment)
        .filter(models.SubscriptionPayment.user_id == user_id)
        .order_by(models.SubscriptionPayment.id.desc())
        .first()
    )


def _account_plan_choice(db: Session, user: models.User):
    """The plan + period this gym has chosen: its latest payment's if
    it has ever paid, otherwise the entry-flow choice in settings."""
    latest = _latest_payment_for(db, user.id)
    if latest is not None:
        return latest.plan_id, latest.period
    return (
        get_setting(db, "plan_id", "pro") or "pro",
        get_setting(db, "plan_period", "monthly") or "monthly",
    )


def _account_subscription(db: Session, user: models.User) -> dict:
    """One account's subscription for Control: chosen plan/period and
    status active | not_paid | expired | switched_off, computed from
    THAT account's own latest payment with the same 30/365-day windows
    as subscription_info(). An account with no payment rows of its own
    is simply not_paid — there is no install-wide fallback (security
    pass, 2026-10-23). The switch-off override always wins."""
    plan_id, period = _account_plan_choice(db, user)
    plan = get_subscription_plan(plan_id) or get_subscription_plan("pro")
    latest = _latest_payment_for(db, user.id)
    today = date.today()
    if latest is not None:
        if latest.status != "paid":
            status, active, expires_on = "not_paid", False, None
        else:
            start = latest.created_at.date() if latest.created_at else today
            end = _period_end(start, latest.period)
            expires_on = end.isoformat()
            if today < end:
                status, active = "active", True
            else:
                status, active = "expired", False
    else:
        # No payment rows of its own: NOT paid. (An earlier version
        # fell back to the install-wide state here, which let a
        # never-paid account ride on another account's payment —
        # removed in the 2026-10-23 security pass.)
        status, active, expires_on = "not_paid", False, None
    if is_revoked(db, user.id):
        status, active = "switched_off", False
    return {
        "plan_id": plan["id"],
        "plan_name": plan["name"],
        "period": period,
        "status": status,
        "active": active,
        "expires_on": expires_on,
    }


def _account_dict(db: Session, user: models.User) -> dict:
    """Everything Farhan sees about one gym on Control."""
    return {
        "user_id": user.id,
        "email": user.email,
        "owner_name": user.owner_name or "",
        "gym_name": user.gym_name or "",
        "mobile": user.mobile or "",
        "auth_provider": user.auth_provider or "email",
        "otp_pending": bool(getattr(user, "otp_pending", False)),
        "joined": user.created_at.isoformat() if user.created_at else None,
        **_account_subscription(db, user),
    }


def _admin_summary(db: Session) -> dict:
    users = db.query(models.User).order_by(models.User.id).all()
    accounts = [_account_dict(db, u) for u in users]
    total_collected = (
        db.query(func.sum(models.SubscriptionPayment.amount))
        .filter(models.SubscriptionPayment.status == "paid")
        .scalar()
    ) or 0
    return {
        "total_gyms": len(accounts),
        "active_gyms": sum(1 for a in accounts if a["status"] == "active"),
        "total_collected": int(total_collected),
        "accounts": accounts,
    }


def _find_account(db: Session, user_id, email):
    if user_id is not None:
        return db.query(models.User).filter(models.User.id == user_id).first()
    if email and str(email).strip():
        return (
            db.query(models.User)
            .filter(models.User.email == str(email).strip().lower())
            .first()
        )
    return None


def _activate_account(db: Session, user: models.User, plan_id=None, period=None):
    """Farhan's one-tap activation after collecting in person.

    Clears any switch-off override first. Then, if the gym is already
    ACTIVE on the very plan being activated, NOTHING more is recorded
    — the money for this window was already collected once, and
    recording again would inflate the collected total. In every other
    case — unpaid, expired, switched off with a lapsed window, or a
    NEW plan/period being collected for — a fresh payment goes
    through the standard recording path (server-computed amount) with
    method "collected_in_person", and the gym's gate lifts at once.
    Returns {"recorded_payment": bool, "account": {...}} or None when
    the plan is unknown.
    """
    chosen_plan, chosen_period = _account_plan_choice(db, user)
    plan_id = plan_id or chosen_plan
    period = period or chosen_period
    if get_subscription_plan(plan_id) is None:
        return None
    if is_revoked(db, user.id):
        set_revoked(db, user.id, False)
        db.commit()
        logger.info("Admin cleared the switch-off for %s", user.email)
    info = _account_subscription(db, user)
    if info["active"] and info["plan_id"] == plan_id and info["period"] == period:
        logger.info(
            "Admin re-activated %s — paid window still valid, no new payment recorded",
            user.email,
        )
        return {"recorded_payment": False, "account": _account_dict(db, user)}
    state = _record_subscription_payment(
        db, user, plan_id, period, method="collected_in_person"
    )
    if state is None:
        return None
    return {"recorded_payment": True, "account": _account_dict(db, user)}


def _admin_auth(request: Request):
    """Admin credential check for the API. Returns None when admin is
    not configured (routes answer 404), False for bad/missing
    credentials (403), or {"via": "secret" | "cookie", ...}."""
    if not _admin_enabled():
        return None
    if _secret_matches(request.headers.get("x-admin-secret")):
        return {"via": "secret"}
    payload = read_admin_token(request.cookies.get(ADMIN_COOKIE, ""))
    if payload:
        return {"via": "cookie", "csrf": payload.get("csrf", "")}
    return False


def _admin_api_guard(request: Request):
    """(auth, None) when the caller is Farhan; (None, response) otherwise."""
    auth = _admin_auth(request)
    if auth is None:
        return None, err(404, "Not found.")
    if not auth:
        return None, err(403, "Admin access denied.")
    return auth, None


def _admin_csrf_ok(auth: dict, csrf_value) -> bool:
    """Cookie-authenticated POSTs must echo the cookie's CSRF value;
    the secret header is itself the credential, so it needs none."""
    if auth.get("via") != "cookie":
        return True
    return hmac.compare_digest(str(csrf_value or ""), str(auth.get("csrf", "")))


class AdminActivateIn(BaseModel):
    user_id: int | None = None
    email: str = Field(default="", max_length=255)
    plan_id: str | None = Field(default=None, pattern="^(starter|pro)$")
    period: str | None = Field(default=None, pattern="^(monthly|yearly)$")
    csrf: str = Field(default="", max_length=256)
    # Face-verification proof (required on the browser/cookie path —
    # one rule everywhere: no face, no unlock). The X-Admin-Secret
    # header path is the documented break-glass and skips this.
    face_challenge_id: str = Field(default="", max_length=128)
    face_distance: float | None = None
    face_liveness: str = Field(default="", max_length=32)


@app.get("/api/admin/gyms")
def admin_gyms(request: Request, db: Session = Depends(get_db)):
    """Every gym account + subscription status + collected totals."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    return _admin_summary(db)


@app.post("/api/admin/activate")
def admin_activate(body: AdminActivateIn, request: Request, db: Session = Depends(get_db)):
    """Record an in-person collection for one gym and unlock it.

    plan_id/period may be omitted — the gym's chosen plan is used.

    ONE RULE EVERYWHERE (owner's order, 2026-10-23): no face, no
    unlock. On the browser path (admin session cookie — i.e. anything
    a UI can call) the request must carry a passed face verification
    (challenge + liveness + distance from /api/admin/face-challenge).
    The X-Admin-Secret header path is the documented BREAK-GLASS for
    scripts/emergencies only — no UI links to it — and is the single
    exception."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    if not _admin_csrf_ok(auth, body.csrf):
        return err(403, "Admin session check failed. Please reload the control page.")
    if auth.get("via") == "cookie":
        problem = _check_admin_face_proof(
            body.face_challenge_id, body.face_distance, body.face_liveness
        )
        if problem is not None:
            logger.warning("Admin activation refused (no face proof): %s", problem)
            return JSONResponse(
                status_code=403, content={"detail": problem, "code": "face_required"}
            )
    user = _find_account(db, body.user_id, body.email)
    if user is None:
        return err(404, "Gym account not found.")
    result = _activate_account(db, user, body.plan_id, body.period)
    if result is None:
        return err(404, "Plan not found.")
    return {"ok": True, **result, "summary": _admin_summary(db)}


@app.post("/api/admin/switch-off")
def admin_switch_off(body: AdminActivateIn, request: Request, db: Session = Depends(get_db)):
    """Switch one gym OFF: its gate locks at once (even inside a paid
    window). NOTHING is deleted — Activate restores it as it was."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    if not _admin_csrf_ok(auth, body.csrf):
        return err(403, "Admin session check failed. Please reload the control page.")
    user = _find_account(db, body.user_id, body.email)
    if user is None:
        return err(404, "Gym account not found.")
    set_revoked(db, user.id, True)
    db.commit()
    logger.info("Admin SWITCHED OFF %s — locked immediately, all data kept", user.email)
    return {"ok": True, "account": _account_dict(db, user), "summary": _admin_summary(db)}


# ---------------------------------------------------------------------------
# Control support actions (2026-10-23) — Farhan's rescue tools, for the
# day an owner's emailed code never arrives (spam folder, dead inbox,
# SMTP trouble): email must never be a hard blocker when Farhan is
# standing in the gym with the owner in front of him. Same guard as
# every other admin API (ADMIN_SECRET unset → 404; secret header OR
# the /control admin session cookie + its CSRF value), plus the shared
# login rate limiter. Every action writes an AdminAction audit row —
# action, target account, timestamp; NEVER a password or a code.
# ---------------------------------------------------------------------------
def _record_admin_action(db: Session, action: str, user: models.User, detail: str = "") -> None:
    db.add(models.AdminAction(
        action=action,
        target_user_id=user.id,
        target_email=user.email,
        detail=detail,
    ))


class AdminVerifyEmailIn(BaseModel):
    csrf: str = Field(default="", max_length=256)


class AdminTempPasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    csrf: str = Field(default="", max_length=256)


def _admin_support_guard(body_csrf: str, request: Request):
    """Shared gate for the support actions: (auth, None) for Farhan,
    else (None, response) — rate limit, admin guard and CSRF, in the
    same shape as the other admin endpoints."""
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited("admin-support:" + ip):
        return None, err(429, "Too many attempts. Please wait a minute and try again.")
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return None, deny
    if not _admin_csrf_ok(auth, body_csrf):
        return None, err(403, "Admin session check failed. Please reload the control page.")
    return auth, None


@app.post("/api/control/gyms/{user_id}/verify-email")
def control_verify_email(
    user_id: int, body: AdminVerifyEmailIn, request: Request, db: Session = Depends(get_db)
):
    """Mark one account's email verified from Control: exactly the
    end-state of a successful OTP entry — otp_pending cleared and the
    pending OTP challenge deleted — so the owner walks straight on to
    details/payment. Only meaningful while the account IS pending;
    anything else is a clear 400 (nothing to rescue)."""
    _auth, deny = _admin_support_guard(body.csrf, request)
    if deny is not None:
        return deny
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        return err(404, "Gym account not found.")
    if not getattr(user, "otp_pending", False):
        return err(400, "This account's email is already verified — no OTP is pending for it.")
    row = _otp_challenge(db, user)
    if row is not None:
        db.delete(row)
    user.otp_pending = False
    _record_admin_action(db, "verify_email", user)
    db.commit()
    logger.info("Admin verified the email of %s from Control (OTP stage cleared)", user.email)
    return {"ok": True, "account": _account_dict(db, user), "summary": _admin_summary(db)}


@app.post("/api/control/gyms/{user_id}/temp-password")
def control_temp_password(
    user_id: int, body: AdminTempPasswordIn, request: Request, db: Session = Depends(get_db)
):
    """Set a temporary password for one account from Control — same
    semantics as an emailed reset: the hash is replaced (bcrypt, the
    signup path's helper, the signup minimum length) and the session
    epoch is bumped so every existing session dies at once. The
    account's otp_pending state is deliberately NOT touched: a
    still-pending account still owes its OTP (or a Verify email here),
    exactly like after a normal password reset."""
    _auth, deny = _admin_support_guard(body.csrf, request)
    if deny is not None:
        return deny
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        return err(404, "Gym account not found.")
    if len(body.password) < 6:
        return err(400, "Please choose a password of at least 6 characters.")
    user.password_hash = hash_password(body.password)
    user.session_epoch = int(getattr(user, "session_epoch", 0) or 0) + 1
    _record_admin_action(db, "temp_password", user)
    db.commit()
    logger.info(
        "Admin set a temporary password for %s from Control — all sessions invalidated",
        user.email,
    )
    return {"ok": True, "account": _account_dict(db, user), "summary": _admin_summary(db)}


# ---------------------------------------------------------------------------
# The Control page (GET /control) — Farhan's own screen, same Do Gym
# theme, phone-friendly, server-rendered. Login is the ADMIN_SECRET
# itself as a password (rate-limited like owner login); success sets
# the signed admin cookie. Every action is a same-site form POST
# carrying the CSRF value bound into that cookie.
# ---------------------------------------------------------------------------
_CONTROL_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0b;color:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:20px 14px 48px}
.wrap{max-width:560px;margin:0 auto}
.wrap.narrow{max-width:400px;margin-top:8vh}
.brand{font-size:26px;font-weight:900;letter-spacing:-.01em}
.brand span{background:linear-gradient(120deg,#0a84ff,#2ea8ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.muted{color:#9aa3ad;font-size:13.5px;line-height:1.55}
.card{background:#141519;border:1px solid #26282e;border-radius:18px;padding:18px;margin-top:14px}
label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}
input[type=password]{width:100%;background:#0d0e11;border:1px solid #2c2f36;border-radius:12px;color:#f5f7fa;font-size:16px;padding:12px 13px;margin-bottom:13px}
.btn{display:inline-block;width:100%;border:0;border-radius:12px;padding:13px 16px;font-size:15px;font-weight:800;cursor:pointer;text-align:center}
.btn-grad{background:linear-gradient(120deg,#0a84ff,#2ea8ff);color:#fff}
.btn-off{background:transparent;color:#ff8585;border:1.5px solid rgba(255,90,90,.55);width:auto;padding:9px 16px;font-size:13.5px}
.btn-off.armed{background:#e03131;color:#fff;border-color:#e03131}
.btn-on{background:linear-gradient(120deg,#0a84ff,#2ea8ff);color:#fff;width:auto;padding:9px 18px;font-size:13.5px}
.btn-logout{background:transparent;color:#9aa3ad;border:1px solid #2c2f36;width:auto;padding:7px 13px;font-size:12.5px}
.error{background:rgba(224,49,49,.13);border:1px solid rgba(224,49,49,.5);color:#ffa8a8;border-radius:12px;padding:11px 13px;font-size:13.5px;margin-top:14px}
.flash{background:rgba(34,197,94,.13);border:1px solid rgba(34,197,94,.5);color:#86efac;border-radius:12px;padding:11px 13px;font-size:14px;font-weight:700;margin-top:14px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.summary{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:16px}
.stat{background:#141519;border:1px solid #26282e;border-radius:16px;padding:14px 12px;text-align:center}
.stat b{display:block;font-size:21px;font-weight:900}
.stat span{font-size:11.5px;color:#9aa3ad;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.gym-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.gym-name{font-size:17px;font-weight:800}
.tag{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.03em;padding:3px 9px;border-radius:99px;background:#23252b;color:#c6ccd4;border:1px solid #33363e;white-space:nowrap}
.tag-g{background:rgba(10,132,255,.15);color:#7cc0ff;border-color:rgba(10,132,255,.45)}
.status{font-size:12px;font-weight:900;letter-spacing:.03em;padding:4px 11px;border-radius:99px;text-transform:uppercase;white-space:nowrap}
.st-active{background:rgba(34,197,94,.14);color:#4ade80;border:1px solid rgba(34,197,94,.45)}
.st-due{background:rgba(245,158,11,.14);color:#fbbf24;border:1px solid rgba(245,158,11,.5)}
.st-off{background:rgba(224,49,49,.14);color:#ff8585;border:1px solid rgba(224,49,49,.5)}
.kv{font-size:13.5px;color:#c6ccd4;line-height:1.65;margin-top:8px}
.kv b{color:#f5f7fa}
.act{margin-top:12px}
.foot{margin-top:22px;text-align:center}
"""


# Face enrollment on the Control page: lazy-loads face-api from
# /static/face (vendored — no CDN), walks Farhan through three poses,
# and posts ONLY the numeric descriptors to /api/admin/face-template.
_CONTROL_FACE_JS = """
(function(){
  var btn = document.getElementById("face-reg-btn");
  if (!btn) return;
  var box = document.getElementById("face-reg"),
      video = document.getElementById("ctl-face-video"),
      step = document.getElementById("ctl-face-step"),
      start = document.getElementById("ctl-face-start"),
      stream = null, modelsOk = false;
  btn.addEventListener("click", function(){
    box.style.display = box.style.display === "none" ? "block" : "none";
  });
  function loadLib(){
    return new Promise(function(res, rej){
      if (window.faceapi) return res();
      var s = document.createElement("script");
      s.src = "/static/face/face-api.min.js";
      s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  async function loadModels(){
    if (modelsOk) return;
    await loadLib();
    await faceapi.nets.tinyFaceDetector.loadFromUri("/static/face");
    await faceapi.nets.faceLandmark68Net.loadFromUri("/static/face");
    await faceapi.nets.faceRecognitionNet.loadFromUri("/static/face");
    modelsOk = true;
  }
  function wait(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  async function capturePose(label){
    step.textContent = label + " — hold still…";
    var det = null, tries = 0;
    while (tries < 48 && !det) {
      det = await faceapi.detectSingleFace(video, new faceapi.TinyFaceDetectorOptions()).withFaceLandmarks().withFaceDescriptor();
      if (!det) { tries++; await wait(250); }
    }
    if (!det) throw new Error("No face found. Face the camera in good light and try again.");
    return Array.from(det.descriptor);
  }
  start.addEventListener("click", async function(){
    start.disabled = true;
    try {
      step.textContent = "Loading the face tools…";
      await loadModels();
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
      video.srcObject = stream;
      await video.play();
      var poses = [];
      poses.push(await capturePose("Pose 1 of 3: look straight at the camera"));
      poses.push(await capturePose("Pose 2 of 3: turn your head a little to YOUR left"));
      poses.push(await capturePose("Pose 3 of 3: turn your head a little to YOUR right"));
      step.textContent = "Saving your face template…";
      var csrf = document.querySelector('input[name="csrf"]').value;
      var r = await fetch("/api/admin/face-template", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ descriptors: poses, csrf: csrf })
      });
      if (!r.ok) throw new Error("Saving failed. Please try again.");
      location.href = "/control?msg=" + encodeURIComponent("Your face is registered ✓ — gyms can now unlock with your face scan.");
    } catch (e) {
      step.textContent = (e && e.message) ? e.message : "Something went wrong. Please try again.";
      start.disabled = false;
    } finally {
      if (stream) { stream.getTracks().forEach(function(t){ t.stop(); }); stream = null; }
    }
  });

  /* ---- Activation scan: one rule everywhere — no face, no unlock.
     Submitting an Activate form first runs this scan on THIS device;
     only a passed verification fills the form's proof fields and lets
     it submit. The server re-checks the proof (single-use challenge,
     liveness, distance) and records nothing without it. ---- */
  function faceDistance(a, b){
    var s = 0;
    for (var i = 0; i < a.length; i++) { var d = a[i] - b[i]; s += d * d; }
    return Math.sqrt(s);
  }
  function eyeAR(eye){
    function dist(p, q){ return Math.hypot(p.x - q.x, p.y - q.y); }
    return (dist(eye[1], eye[5]) + dist(eye[2], eye[4])) / (2 * dist(eye[0], eye[3]));
  }
  var scanPage = document.getElementById("ctl-scan-page"),
      dash = document.getElementById("ctl-dash"),
      scanGym = document.getElementById("ctl-scan-gym"),
      scanVideo = document.getElementById("ctl-scan-video"),
      scanStep = document.getElementById("ctl-scan-step"),
      scanCancel = document.getElementById("ctl-scan-cancel"),
      scanStream = null, pendingForm = null;
  function stopScanStream(){
    if (scanStream) { scanStream.getTracks().forEach(function(t){ t.stop(); }); scanStream = null; }
  }
  if (scanCancel) scanCancel.addEventListener("click", function(){
    pendingForm = null;
    stopScanStream();
    if (scanPage) scanPage.style.display = "none";
    if (dash) dash.style.display = "";
  });
  async function runActivationScan(form){
    pendingForm = form;
    // The DEDICATED scan page replaces the dashboard (owner's flow
    // order): Control -> Activate -> "Scan Your Face" page -> verified
    // -> the form submits and Control returns with the gym Active.
    if (scanGym) scanGym.textContent = form.dataset.gym || "the gym";
    if (dash) dash.style.display = "none";
    if (scanPage) scanPage.style.display = "block";
    window.scrollTo(0, 0);
    try {
      scanStep.textContent = "Loading the face tools…";
      await loadModels();
      var csrfEl = form.querySelector('input[name="csrf"]');
      var chR = await fetch("/api/admin/face-challenge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: csrfEl ? csrfEl.value : "" })
      });
      if (!chR.ok) {
        var ej = {};
        try { ej = await chR.json(); } catch (e) {}
        throw new Error(ej.detail || "Could not start the face scan.");
      }
      var ch = await chR.json();
      var tplR = await fetch("/api/admin/face-template");
      var tpl = await tplR.json();
      if (!tpl.enrolled) throw new Error("Enroll your face first — activations need your face scan.");
      var enrolled = tpl.descriptors.map(function(d){ return Float32Array.from(d); });
      scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
      scanVideo.srcObject = scanStream;
      await scanVideo.play();
      var actionText = { blink: "please BLINK once", turn_left: "please turn your head LEFT", turn_right: "please turn your head RIGHT" }[ch.action] || "please blink once";
      scanStep.textContent = "Activation scan: look at the camera — then " + actionText + ".";
      var deadline = Date.now() + 45000, earOpen = null, blinkClosed = false, livenessDone = false;
      while (Date.now() < deadline) {
        if (!pendingForm) return; // cancelled
        var det = await faceapi.detectSingleFace(scanVideo, new faceapi.TinyFaceDetectorOptions()).withFaceLandmarks().withFaceDescriptor();
        if (det) {
          var dist = Math.min.apply(null, enrolled.map(function(d){ return faceDistance(det.descriptor, d); }));
          var lm = det.landmarks;
          if (ch.action === "blink") {
            var ear = (eyeAR(lm.getLeftEye()) + eyeAR(lm.getRightEye())) / 2;
            if (earOpen === null || ear > earOpen) earOpen = ear;
            if (earOpen > 0.24 && ear < earOpen * 0.72) blinkClosed = true;
            if (blinkClosed && ear > earOpen * 0.85) livenessDone = true;
          } else {
            var nosePts = lm.getNose();
            var nose = nosePts[3] || nosePts[0];
            var dbox = det.detection.box;
            var offset = (nose.x - (dbox.x + dbox.width / 2)) / dbox.width;
            if (ch.action === "turn_left" && offset < -0.07) livenessDone = true;
            if (ch.action === "turn_right" && offset > 0.07) livenessDone = true;
          }
          if (dist <= ch.threshold && livenessDone) {
            form.querySelector('input[name="face_challenge_id"]').value = ch.challenge_id;
            form.querySelector('input[name="face_distance"]').value = String(dist);
            form.querySelector('input[name="face_liveness"]').value = ch.action;
            scanStep.textContent = "Face verified ✓ — activating…";
            stopScanStream();
            form.submit();
            return;
          }
          scanStep.textContent = dist <= ch.threshold
            ? "Face matched — now " + actionText + "."
            : "Activation scan: looking for your face — then " + actionText + ".";
        }
        await wait(200);
      }
      scanStep.textContent = "That took too long. Tap Activate to try the scan again.";
      stopScanStream();
    } catch (e) {
      scanStep.textContent = (e && e.message) ? e.message : "The face scan could not run. Please try again.";
      stopScanStream();
    }
  }
  document.querySelectorAll("form.activate-form").forEach(function(form){
    form.addEventListener("submit", function(ev){
      if (form.querySelector('input[name="face_challenge_id"]').value) return; // proof filled — let it go
      ev.preventDefault();
      runActivationScan(form);
    });
  });
})();
"""


def _control_login_html(error: str = "") -> str:
    err_box = f'<div class="error">{html_escape(error)}</div>' if error else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Do Gym — Control</title><style>" + _CONTROL_CSS + "</style></head>"
        '<body><div class="wrap narrow">'
        '<div class="brand">Do <span>Gym</span> — Control</div>'
        '<p class="muted" style="margin-top:8px">This page is only for the owner of Do Gym. '
        "Enter your admin password to see every gym, activate the payments "
        "you collect in person, and switch gyms on or off.</p>"
        + err_box
        + '<form method="post" action="/control/login" class="card">'
        f'<input type="hidden" name="csrf" value="{create_csrf_token()}">'
        '<label for="pw">Admin password</label>'
        '<input id="pw" type="password" name="password" autocomplete="current-password" required>'
        '<button class="btn" type="button" style="margin:0 0 12px" onclick="var i=document.getElementById(\'pw\');var s=i.type===\'password\';i.type=s?\'text\':\'password\';this.textContent=s?\'Hide password\':\'Show password\'">Show password</button>'
        '<button class="btn btn-grad" type="submit">Open Control</button>'
        "</form></div></body></html>"
    )


_ADMIN_STATUS_LABELS = {
    "active": ("Active ✓", "st-active"),
    "not_paid": ("Not Paid", "st-due"),
    "expired": ("Expired", "st-due"),
    "switched_off": ("Switched Off", "st-off"),
}


def _control_dashboard_html(db: Session, csrf: str, msg: str = "") -> str:
    summary = _admin_summary(db)
    e = html_escape
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Do Gym — Control</title><style>" + _CONTROL_CSS + "</style></head>"
        '<body><div class="wrap"><div id="ctl-dash">'
        '<div class="topbar"><div class="brand">Do <span>Gym</span> — Control</div>'
        '<form method="post" action="/control/logout">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<button class="btn btn-logout" type="submit">Logout</button></form></div>'
        '<p class="muted" style="margin-top:6px">Every gym on Do Gym. When you collect a '
        "payment in person, tap <b>Activate</b> — that gym's app turns green and opens "
        "by itself. <b>Switch Off</b> locks a gym that has not paid; nothing is deleted.</p>",
    ]
    if msg:
        parts.append(f'<div class="flash">{e(msg)}</div>')
    parts.append(
        '<div class="summary">'
        f'<div class="stat"><b>{summary["total_gyms"]}</b><span>Total gyms</span></div>'
        f'<div class="stat"><b>{summary["active_gyms"]}</b><span>Active now</span></div>'
        f'<div class="stat"><b>₹{_inr(summary["total_collected"])}</b><span>Collected</span></div>'
        "</div>"
    )
    tpl = _face_template(db)
    if tpl is not None:
        face_inner = (
            '<span class="tag tag-g">Registered ✓</span></div>'
            f'<div class="kv">{len(tpl["descriptors"])} pose(s) on file. When you visit a gym, '
            'the owner taps Pay and your face scan on their device unlocks '
            "their app; Activate here asks for the same scan on this device. "
            "Only the numeric template is stored — never a photo. "
            "Re-registering replaces it.</div>"
        )
        face_btn = "Re-register my face"
    else:
        face_inner = (
            '<span class="tag">Not registered</span></div>'
            '<div class="kv">Register once — then, when you visit a gym, the owner taps '
            'Pay and your face scan on their device unlocks their app; Activate here '
            "asks for the same scan on this device. "
            "Only the numeric template is stored — never a photo.</div>"
        )
        face_btn = "Register my face"
    parts.append(
        '<div class="card"><div class="gym-head"><div class="gym-name">Your face — gym unlock</div>'
        + face_inner
        + f'<div class="act"><button class="btn btn-on" id="face-reg-btn" type="button">{face_btn}</button></div>'
        '<div id="face-reg" style="display:none;margin-top:14px">'
        '<video id="ctl-face-video" autoplay playsinline muted style="width:100%;border-radius:14px;background:#000"></video>'
        '<p class="kv" id="ctl-face-step">Tap "Start camera", then follow the three poses.</p>'
        '<div class="act"><button class="btn btn-grad" id="ctl-face-start" type="button">Start camera</button></div>'
        "</div>"
        "</div>"
    )
    for a in summary["accounts"]:
        label, cls = _ADMIN_STATUS_LABELS[a["status"]]
        tag = (
            '<span class="tag tag-g">Google ✓ verified</span>'
            if a["auth_provider"] == "google"
            else '<span class="tag">Email</span>'
        )
        if a["status"] == "active":
            expiry = f'Active till {e(_fmt_date(a["expires_on"]))}'
        elif a["status"] == "expired":
            expiry = f'Expired on {e(_fmt_date(a["expires_on"]))}'
        elif a["status"] == "switched_off":
            expiry = "Switched off by you — all data is safe; Activate restores it."
            if a["expires_on"]:
                expiry += f' Paid window was till {e(_fmt_date(a["expires_on"]))}.'
        else:
            expiry = "No payment recorded yet."
        if a["status"] == "active":
            action = (
                '<form method="post" action="/control/switch-off" class="switch-off act" '
                f'data-gym="{e(a["gym_name"] or a["email"])}">'
                f'<input type="hidden" name="csrf" value="{e(csrf)}">'
                f'<input type="hidden" name="user_id" value="{a["user_id"]}">'
                '<button class="btn btn-off" type="submit">Switch Off</button></form>'
            )
        else:
            action = (
                '<form method="post" action="/control/activate" class="act activate-form" '
                f'data-gym="{e(a["gym_name"] or a["email"])}">'
                f'<input type="hidden" name="csrf" value="{e(csrf)}">'
                f'<input type="hidden" name="user_id" value="{a["user_id"]}">'
                '<input type="hidden" name="face_challenge_id" value="">'
                '<input type="hidden" name="face_distance" value="">'
                '<input type="hidden" name="face_liveness" value="">'
                '<button class="btn btn-on" type="submit">Activate</button></form>'
            )
        if a["otp_pending"]:
            otp_line = (
                '<br>Email OTP: <b>pending</b> — the owner has not entered '
                "the emailed code. If it never arrived, tap Verify email below."
            )
        else:
            otp_line = ""
        support = ""
        if a["otp_pending"]:
            support += (
                '<form method="post" class="act support-verify" '
                f'data-user-id="{a["user_id"]}" data-csrf="{e(csrf)}">'
                '<button class="btn btn-on" type="submit">Verify email</button></form>'
            )
        support += (
            '<form method="post" class="act support-temp-pw" '
            f'data-user-id="{a["user_id"]}" data-csrf="{e(csrf)}">'
            '<input type="password" name="temp_password" autocomplete="new-password" '
            'placeholder="Temporary password (6+ characters)">'
            '<button class="btn btn-on" type="submit">Set temp password</button></form>'
        )
        parts.append(
            '<div class="card"><div class="gym-head"><div>'
            f'<div class="gym-name">{e(a["gym_name"] or "—")}</div>'
            f'<div style="margin-top:5px">{tag}</div></div>'
            f'<span class="status {cls}">{label}</span></div>'
            '<div class="kv">'
            f'Owner: <b>{e(a["owner_name"] or "—")}</b> · {e(a["email"])}<br>'
            f'Mobile: <b>{e(a["mobile"] or "—")}</b> · Joined: <b>{e(_fmt_date(a["joined"]))}</b><br>'
            f'Plan: <b>{e(a["plan_name"])} ({e(a["period"])})</b> · {e(expiry)}'
            + otp_line
            + "</div>" + action + support + "</div>"
        )
    if not summary["accounts"]:
        parts.append('<div class="card"><p class="muted">No gym accounts yet.</p></div>')
    parts.append(
        "</div>"
        '<div id="ctl-scan-page" style="display:none">'
        '<div class="topbar"><div class="brand">Do <span>Gym</span> — Control</div></div>'
        '<h1 style="font-size:24px;margin:10px 0 4px">Scan Your Face</h1>'
        '<p class="muted">Activating <b id="ctl-scan-gym"></b> — look at the camera and '
        "complete the movement asked. Nothing is recorded until your face passes; "
        "cancelling records nothing.</p>"
        '<video id="ctl-scan-video" autoplay playsinline muted style="width:100%;border-radius:14px;background:#000;margin-top:12px"></video>'
        '<p class="kv" id="ctl-scan-step" style="margin-top:10px">Face scan for activation…</p>'
        '<div class="act"><button class="btn btn-ghost" id="ctl-scan-cancel" type="button">Cancel — back to Control</button></div>'
        "</div>"
    )
    parts.append("<script>" + _CONTROL_FACE_JS + "</script>")
    parts.append(
        '<p class="muted foot">Activations are recorded as collected in person. '
        "Switching a gym off never deletes its members or data.</p>"
        "<script>"
        'document.querySelectorAll("form.switch-off").forEach(function(f){'
        'f.addEventListener("submit",function(ev){var b=f.querySelector("button");'
        'if(b.dataset.armed)return;ev.preventDefault();b.dataset.armed="1";'
        'b.textContent="Tap again to confirm — switch off "+f.dataset.gym+"?";'
        'b.classList.add("armed");});});'
        "</script>"
        "<script>"
        'function ctlSupport(url,payload,okMsg){'
        'fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify(payload)})'
        '.then(function(r){return r.json().then(function(j){'
        'var m=r.ok?okMsg:(j&&j.detail?j.detail:"That did not work. Please try again.");'
        'location.href="/control?msg="+encodeURIComponent(m);});})'
        '.catch(function(){location.href="/control?msg="'
        '+encodeURIComponent("Network problem — please try again.");});}'
        'document.querySelectorAll("form.support-verify").forEach(function(f){'
        'f.addEventListener("submit",function(ev){ev.preventDefault();'
        'ctlSupport("/api/control/gyms/"+f.dataset.userId+"/verify-email",'
        '{csrf:f.dataset.csrf},'
        '"Email verified \\u2713 — the owner can log in and continue.");});});'
        'document.querySelectorAll("form.support-temp-pw").forEach(function(f){'
        'f.addEventListener("submit",function(ev){ev.preventDefault();'
        'var pw=f.querySelector("input[name=temp_password]").value;'
        'ctlSupport("/api/control/gyms/"+f.dataset.userId+"/temp-password",'
        '{csrf:f.dataset.csrf,password:pw},'
        '"Temporary password set \\u2713 — all old sessions signed out.");});});'
        "</script></div></body></html>"
    )
    return "".join(parts)


def _behind_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        or os.environ.get("COOKIE_SECURE", "") == "1"
    )


def _set_admin_cookies(resp: Response, request: Request) -> None:
    """One signed admin token, set once per scope (page + admin API
    + control support API)."""
    token, _csrf = create_admin_token()
    for path in ("/control", "/api/admin", "/api/control"):
        resp.set_cookie(
            ADMIN_COOKIE, token, max_age=ADMIN_MAX_AGE,
            httponly=True, samesite="lax", path=path,
            secure=_behind_https(request),
        )


def _clear_admin_cookies(resp: Response) -> None:
    for path in ("/control", "/api/admin", "/api/control"):
        resp.delete_cookie(ADMIN_COOKIE, path=path)


def _admin_session(request: Request):
    return read_admin_token(request.cookies.get(ADMIN_COOKIE, ""))


@app.get("/control", response_class=HTMLResponse)
def control_page(request: Request, db: Session = Depends(get_db)):
    if not _admin_enabled():
        return Response("Not found.", status_code=404, media_type="text/plain")
    sess = _admin_session(request)
    if sess is None:
        return HTMLResponse(_control_login_html())
    return HTMLResponse(
        _control_dashboard_html(db, sess.get("csrf", ""), request.query_params.get("msg", ""))
    )


@app.post("/control/login")
def control_login(request: Request, password: str = Form(""), csrf: str = Form("")):
    if not _admin_enabled():
        return Response("Not found.", status_code=404, media_type="text/plain")
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited("admin:" + ip):
        return HTMLResponse(
            _control_login_html("Too many attempts. Please wait a minute and try again."),
            status_code=429,
        )
    if not verify_csrf_token(csrf):
        return HTMLResponse(
            _control_login_html("That form has expired. Please try again."),
            status_code=400,
        )
    if not _secret_matches(password):
        logger.warning("Control page: wrong admin password from %s", ip)
        return HTMLResponse(
            _control_login_html("Wrong password. Please try again."),
            status_code=401,
        )
    resp = RedirectResponse(url="/control", status_code=303)
    _set_admin_cookies(resp, request)
    logger.info("Control page: admin logged in from %s", ip)
    return resp


def _control_post_guard(request: Request):
    """(session, None) for an authenticated control POST, else
    (None, response): 404 when admin is off, login redirect when the
    session is gone."""
    if not _admin_enabled():
        return None, Response("Not found.", status_code=404, media_type="text/plain")
    sess = _admin_session(request)
    if sess is None:
        return None, RedirectResponse(url="/control", status_code=303)
    return sess, None


def _control_csrf_bad(sess: dict, csrf: str):
    if hmac.compare_digest(str(csrf or ""), str(sess.get("csrf", ""))):
        return None
    return Response(
        "Session check failed. Please reload the control page and try again.",
        status_code=403, media_type="text/plain",
    )


@app.post("/control/activate")
def control_activate(
    request: Request,
    user_id: int = Form(0),
    csrf: str = Form(""),
    face_challenge_id: str = Form(""),
    face_distance: float | None = Form(None),
    face_liveness: str = Form(""),
    db: Session = Depends(get_db),
):
    sess, deny = _control_post_guard(request)
    if deny is not None:
        return deny
    bad = _control_csrf_bad(sess, csrf)
    if bad is not None:
        return bad
    # One rule everywhere: no face, no unlock. Without a passed face
    # verification NOTHING is recorded — the gym stays as it was.
    problem = _check_admin_face_proof(face_challenge_id, face_distance, face_liveness)
    if problem is not None:
        logger.warning("Control activation refused (no face proof): %s", problem)
        return RedirectResponse(
            url="/control?msg=" + quote(problem), status_code=303
        )
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        return RedirectResponse(
            url="/control?msg=" + quote("That gym account was not found."), status_code=303
        )
    _activate_account(db, user)
    name = user.gym_name or user.email
    return RedirectResponse(
        url="/control?msg=" + quote(f"{name} is now Active ✓"), status_code=303
    )


@app.post("/control/switch-off")
def control_switch_off(request: Request, user_id: int = Form(0), csrf: str = Form(""), db: Session = Depends(get_db)):
    sess, deny = _control_post_guard(request)
    if deny is not None:
        return deny
    bad = _control_csrf_bad(sess, csrf)
    if bad is not None:
        return bad
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        return RedirectResponse(
            url="/control?msg=" + quote("That gym account was not found."), status_code=303
        )
    set_revoked(db, user.id, True)
    db.commit()
    logger.info("Control page: SWITCHED OFF %s — all data kept", user.email)
    name = user.gym_name or user.email
    return RedirectResponse(
        url="/control?msg=" + quote(f"{name} is now Switched Off"), status_code=303
    )


@app.post("/control/logout")
def control_logout(request: Request, csrf: str = Form("")):
    sess, deny = _control_post_guard(request)
    if deny is not None:
        return deny
    bad = _control_csrf_bad(sess, csrf)
    if bad is not None:
        return bad
    resp = RedirectResponse(url="/control", status_code=303)
    _clear_admin_cookies(resp)
    return resp


# ---------------------------------------------------------------------------
# Face unlock — Farhan's final unlock decision (2026-10-23). He is
# physically at the gym (he just collected the money in person). The
# owner opens the locked payment screen and taps "Farhan is here —
# Unlock by Face Scan": the OWNER's device camera matches Farhan's face
# against the template he enrolled from Control, with a liveness beat
# (blink / head-turn) against photo spoofing. On a verified match the
# server records the activation through the standard path (method
# "face_unlock", the gym's chosen plan/period, server-computed amount)
# and the pay-first gate lifts — the owner's page polls into the green
# "Payment Done ✓" state and unlocks, exactly like every other route.
#
# The honest security shape (also in README / VERIFICATION §16): the
# face MATCH runs in the browser, so this is convenience-grade, not
# bank-grade — Farhan's informed choice. What the server enforces: a
# short-lived (120s), single-use challenge bound to this owner with a
# SERVER-chosen liveness action; the reported match distance must beat
# the threshold; 5 failed verifications lock face unlock for that gym
# for 15 minutes; Switch Off still overrides a face-unlocked gym; and
# the stored template is numeric descriptors only — never a photo.
# ---------------------------------------------------------------------------
FACE_CHALLENGE_TTL = 120  # seconds
FACE_MAX_FAILS = 5
FACE_LOCKOUT_SECONDS = 15 * 60
FACE_MATCH_THRESHOLD = 0.5  # face-api euclidean distance for a match
FACE_ACTIONS = ("blink", "turn_left", "turn_right")
_face_challenges: dict[str, dict] = {}
_face_fails: dict[int, list[float]] = defaultdict(list)
_face_lockouts: dict[int, float] = {}


def _face_template(db: Session):
    """{"descriptors": [[128 floats], ...], "updated_at": iso} or None."""
    row = db.query(models.FaceTemplate).order_by(models.FaceTemplate.id).first()
    if row is None:
        return None
    try:
        descriptors = json.loads(row.descriptors or "[]")
    except ValueError:
        return None
    if not descriptors:
        return None
    updated = getattr(row, "updated_at", None)
    return {
        "descriptors": descriptors,
        "updated_at": updated.isoformat() if updated else None,
    }


def _face_locked_out(user_id: int) -> bool:
    until = _face_lockouts.get(user_id, 0)
    if until and time.time() < until:
        return True
    if until:
        _face_lockouts.pop(user_id, None)
    return False


def _face_record_fail(user_id: int) -> None:
    now = time.time()
    fails = [t for t in _face_fails[user_id] if now - t < FACE_LOCKOUT_SECONDS]
    fails.append(now)
    if len(fails) >= FACE_MAX_FAILS:
        _face_lockouts[user_id] = now + FACE_LOCKOUT_SECONDS
        _face_fails[user_id] = []
        logger.warning(
            "Face unlock LOCKED OUT for user %s after %d failed attempts",
            user_id, FACE_MAX_FAILS,
        )
    else:
        _face_fails[user_id] = fails


@app.get("/static/face/{name}")
def face_asset(name: str):
    """The vendored face-api library + model weights (served locally —
    no runtime CDN). A strict filename pattern; nothing else in
    static/ is reachable through this route."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or ".." in name:
        return err(404, "Not found.")
    path = STATIC_DIR / "face" / name
    if not path.is_file():
        return err(404, "Not found.")
    if name.endswith(".js"):
        media = "application/javascript"
    elif name.endswith(".json"):
        media = "application/json"
    else:
        media = "application/octet-stream"
    return FileResponse(path, media_type=media)


class FaceTemplateIn(BaseModel):
    descriptors: list = Field(default_factory=list)
    csrf: str = ""


@app.post("/api/admin/face-template")
def admin_face_template_enroll(
    body: FaceTemplateIn, request: Request, db: Session = Depends(get_db)
):
    """Enroll or replace Farhan's face template (numeric descriptors
    only — computed in his browser in Control; no photo ever arrives
    here). Admin: secret header OR the /control session (with CSRF)."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    if not _admin_csrf_ok(auth, body.csrf):
        return err(403, "Admin session check failed. Please reload Control.")
    descriptors = body.descriptors
    if not isinstance(descriptors, list) or not (1 <= len(descriptors) <= 6):
        return err(400, "Please capture between 1 and 6 poses.")
    cleaned = []
    for d in descriptors:
        if (
            not isinstance(d, list)
            or len(d) != 128
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in d)
        ):
            return err(400, "That face data does not look right. Please try again.")
        cleaned.append([round(float(x), 6) for x in d])
    row = db.query(models.FaceTemplate).order_by(models.FaceTemplate.id).first()
    payload = json.dumps(cleaned)
    if row is None:
        db.add(models.FaceTemplate(descriptors=payload, updated_at=datetime.now()))
    else:
        row.descriptors = payload
        row.updated_at = datetime.now()
    db.commit()
    logger.info("Face template (re)enrolled by admin: %d pose(s)", len(cleaned))
    return {"ok": True, "poses": len(cleaned), "face_enrolled": True}


@app.post("/api/admin/face-template/remove")
def admin_face_template_remove(request: Request, db: Session = Depends(get_db)):
    """Remove the enrolled template (face unlock hides everywhere)."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    if not _admin_csrf_ok(auth, None):
        return err(403, "Admin session check failed. Please reload Control.")
    db.query(models.FaceTemplate).delete()
    db.commit()
    logger.info("Face template removed by admin")
    return {"ok": True, "face_enrolled": False}


# ---------------------------------------------------------------------------
# Face-gated ACTIVATION (owner's rule, 2026-10-23): an Activate from
# Control — the page form or the cookie-authenticated API — records
# NOTHING until Farhan's face passes a verification on his own device:
# the page asks this server for a single-use challenge, runs the same
# local match + server-chosen liveness as the owner's Pay button, and
# sends the proof back with the activation. The secret-header API is
# the documented break-glass exception (scripts only, no UI links).
# ---------------------------------------------------------------------------
_admin_face_challenges: dict[str, dict] = {}


class AdminFaceChallengeIn(BaseModel):
    csrf: str = ""


@app.get("/api/admin/face-template")
def admin_face_template_get(request: Request, db: Session = Depends(get_db)):
    """The enrolled descriptors, for the Control page's activation
    scan to match against locally. Admin auth required."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    tpl = _face_template(db)
    if tpl is None:
        return {"enrolled": False, "descriptors": []}
    return {"enrolled": True, "descriptors": tpl["descriptors"]}


@app.post("/api/admin/face-challenge")
def admin_face_challenge(
    body: AdminFaceChallengeIn, request: Request, db: Session = Depends(get_db)
):
    """Issue a single-use, 120-second face-verification challenge for
    a Control activation, with a server-chosen liveness action."""
    auth, deny = _admin_api_guard(request)
    if deny is not None:
        return deny
    if not _admin_csrf_ok(auth, body.csrf):
        return err(403, "Admin session check failed. Please reload Control.")
    if _face_template(db) is None:
        return err(
            400,
            "Enroll your face first — activations need your face scan.",
        )
    cid = secrets.token_urlsafe(24)
    _admin_face_challenges[cid] = {
        "action": secrets.choice(FACE_ACTIONS),
        "expires": time.time() + FACE_CHALLENGE_TTL,
        "used": False,
    }
    return {
        "challenge_id": cid,
        "action": _admin_face_challenges[cid]["action"],
        "expires_in": FACE_CHALLENGE_TTL,
        "threshold": FACE_MATCH_THRESHOLD,
    }


def _check_admin_face_proof(challenge_id, distance, liveness) -> str | None:
    """Validate (and burn) an admin face-verification claim. Returns
    an error message, or None when the proof is good."""
    ch = _admin_face_challenges.get(challenge_id or "")
    if ch is None or ch["used"] or time.time() > ch["expires"]:
        return (
            "Face verification is required — tap Activate and complete "
            "the face scan. Nothing was recorded."
        )
    ch["used"] = True  # single-use — any attempt burns the challenge
    if liveness != ch["action"]:
        return (
            "The liveness check did not match. Nothing was recorded — "
            "please try the face scan again."
        )
    if distance is None or distance > FACE_MATCH_THRESHOLD:
        return (
            "That face did not match closely enough. Nothing was "
            "recorded — please try the face scan again."
        )
    return None


@app.get("/api/face-unlock/template")
def face_unlock_template(
    db: Session = Depends(get_db),
    user: models.User | JSONResponse = Depends(current_user),
):
    """The enrolled descriptors, for the owner's browser to match
    against locally. Owner session required; reachable while gated —
    it is part of the unlock flow itself."""
    if isinstance(user, JSONResponse):
        return user
    tpl = _face_template(db)
    if tpl is None:
        return {"enrolled": False, "descriptors": []}
    return {"enrolled": True, "descriptors": tpl["descriptors"]}


@app.post("/api/face-unlock/challenge")
def face_unlock_challenge(
    db: Session = Depends(get_db),
    user: models.User | JSONResponse = Depends(current_user),
):
    """Start a scan session: a single-use, 120-second challenge bound
    to this owner, with a server-chosen liveness action."""
    if isinstance(user, JSONResponse):
        return user
    if _face_template(db) is None:
        return err(400, "Face unlock is not set up yet.")
    if _face_locked_out(user.id):
        return err(
            429,
            "Too many failed face attempts. Please wait 15 minutes, or "
            "ask Do Gym to activate you from Control.",
        )
    for cid in [c for c, ch in _face_challenges.items() if ch["user_id"] == user.id]:
        _face_challenges.pop(cid, None)
    cid = secrets.token_urlsafe(24)
    _face_challenges[cid] = {
        "user_id": user.id,
        "action": secrets.choice(FACE_ACTIONS),
        "expires": time.time() + FACE_CHALLENGE_TTL,
        "used": False,
    }
    return {
        "challenge_id": cid,
        "action": _face_challenges[cid]["action"],
        "expires_in": FACE_CHALLENGE_TTL,
        "threshold": FACE_MATCH_THRESHOLD,
    }


class FaceVerifyIn(BaseModel):
    challenge_id: str = Field(min_length=8, max_length=128)
    distance: float | None = None
    liveness: str = Field(default="", max_length=32)


@app.post("/api/face-unlock/verify")
def face_unlock_verify(
    body: FaceVerifyIn,
    db: Session = Depends(get_db),
    user: models.User | JSONResponse = Depends(current_user),
):
    """Confirm a scan. The challenge must be live, unused, this
    owner's, with the liveness action the server chose and a match
    distance under the threshold. Any verify attempt burns the
    challenge (single-use). Success records the activation."""
    if isinstance(user, JSONResponse):
        return user
    if _face_locked_out(user.id):
        return err(
            429,
            "Face unlock is locked for 15 minutes after too many "
            "failed attempts. Please ask Do Gym to activate you from Control.",
        )
    if _face_template(db) is None:
        return err(400, "Face unlock is not set up yet.")
    if is_revoked(db, user.id):
        return err(403, "⏸️ Your plan is currently off. Please contact Do Gym to restart.")
    ch = _face_challenges.get(body.challenge_id)
    if (
        ch is None
        or ch["user_id"] != user.id
        or ch["used"]
        or time.time() > ch["expires"]
    ):
        _face_record_fail(user.id)
        return err(400, "That face scan session has expired. Please start again.")
    ch["used"] = True  # single-use — any attempt burns the challenge
    if body.liveness != ch["action"]:
        _face_record_fail(user.id)
        logger.info(
            "Face unlock failed for %s: liveness %r, wanted %r",
            user.email, body.liveness, ch["action"],
        )
        return err(400, "The liveness check did not match. Please try again.")
    if body.distance is None or body.distance > FACE_MATCH_THRESHOLD:
        _face_record_fail(user.id)
        logger.info(
            "Face unlock failed for %s: distance %s over threshold",
            user.email, body.distance,
        )
        return err(400, "That face did not match closely enough. Please try again.")
    # Verified.
    _face_fails.pop(user.id, None)
    if subscription_info(db, user_id=user.id)["active"]:
        state = get_plan_state(db, user_id=user.id)
        state["ok"] = True
        state["already_active"] = True
        return state
    plan_id, period = _account_plan_choice(db, user)
    state = _record_subscription_payment(
        db, user, plan_id, period, method="face_unlock"
    )
    if state is None:
        return err(404, "Plan not found.")
    logger.info(
        "Face unlock ACTIVATED %s -> %s (%s), amount ₹%s via face_unlock",
        user.email, plan_id, period, state["last_payment"]["amount"],
    )
    state["ok"] = True
    return state


def _placeholder_qr_png() -> bytes:
    """A minimal placeholder image (dark card, pink border), generated with
    the standard library only. Used ONLY if backend/static/payment-qr.png
    is missing, so the payment panel never shows a broken image."""
    import struct
    import zlib

    w = h = 480
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            border = x < 14 or y < 14 or x >= w - 14 or y >= h - 14
            raw += bytes((255, 46, 126) if border else (20, 20, 43))

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + body + crc

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


@app.get("/payment-qr.png")
def payment_qr(user: models.User = Depends(current_user)):
    """The owner's UPI payment QR, shown in the in-app payment panel.

    The panel only exists inside the logged-in app, so this requires a
    session. The image is the single file backend/static/payment-qr.png —
    to change the QR, replace that file (see README).
    """
    if isinstance(user, JSONResponse):
        return user
    if PAYMENT_QR_FILE.exists():
        return FileResponse(PAYMENT_QR_FILE, media_type="image/png")
    return Response(content=_placeholder_qr_png(), media_type="image/png")


@app.post("/api/reset")
def reset_demo(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    if not SEED_ENABLED:
        return err(403, "Demo reset is turned off on this server.")
    seed_database(db)
    return {"ok": True, "message": "Demo data has been reset."}


# ---------------------------------------------------------------------------
# Member creation (owner-side). The PHG code is ALWAYS assigned here, from
# the monotonic counter — the owner never types one, duplicates impossible.
# ---------------------------------------------------------------------------
@app.post("/api/members")
def create_member(body: MemberCreateIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    fields: dict[str, str] = {}
    name = body.name.strip()
    phone = clean_phone(body.phone)
    plan = body.plan.strip()
    if len(name) < 2:
        fields["name"] = "Please enter the member's full name."
    # Phone is optional (the owner may not have it at joining time); a
    # number that IS given must be a valid 10-digit mobile.
    if phone and len(phone) != 10:
        fields["phone"] = "Please enter a 10-digit mobile number, or leave it empty."
    if plan not in PLANS:
        fields["plan"] = "Please choose a plan: Monthly, Quarterly or Yearly."
    expiry = None
    if body.expiry_date.strip():
        try:
            expiry = date.fromisoformat(body.expiry_date.strip())
        except ValueError:
            fields["expiry_date"] = "Please enter the expiry date as YYYY-MM-DD."
    if fields:
        return field_error(fields, "Please check the highlighted fields.")
    if expiry is None:
        expiry = date.today() + timedelta(days=PLANS[plan]["days"])
    member = models.Member(
        code=next_member_code(db), name=name, phone=phone, plan=plan,
        expiry_date=expiry,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    logger.info("Member added by %s: %s (%s)", user.email, member.name, member.code)
    return {
        "ok": True,
        "member": member_dict(db, member, date.today()),
        "message": f"{member.name} added — their member code is {member.code}.",
    }


# ---------------------------------------------------------------------------
# QR self check-in (public). The flow the owner asked for, fully automatic:
# the member scans the wall poster with their OWN phone -> /checkin/<token>
# -> types their PHG code once (the phone remembers it and auto-submits on
# every later visit) -> POST /api/checkin/scan writes the CheckIn row. The
# owner does nothing; the dashboard, Red List and today's list all read the
# same checkins table, so they update by themselves.
# ---------------------------------------------------------------------------
def gym_display_name(db: Session) -> str:
    """The gym's name for public pages: the first owner's gym name."""
    owner = db.query(models.User).order_by(models.User.id).first()
    if owner and (owner.gym_name or "").strip():
        return owner.gym_name.strip()
    return "Do Gym"


@app.get("/api/gym/checkin-link")
def gym_checkin_link(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Owner-only: this gym's public check-in URL (what the wall QR encodes)."""
    if isinstance(user, JSONResponse):
        return user
    token = get_or_create_checkin_token(db)
    return {
        "url": f"{app_base_url()}/checkin/{token}",
        "token": token,
        "gym_name": gym_display_name(db),
    }


@app.post("/api/gym/checkin-link/reset")
def gym_checkin_link_reset(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """Owner-only: a fresh token — every previously printed poster dies."""
    if isinstance(user, JSONResponse):
        return user
    token = reset_checkin_token(db)
    logger.info("Check-in link reset by %s (old QR posters retired)", user.email)
    return {
        "url": f"{app_base_url()}/checkin/{token}",
        "token": token,
        "gym_name": gym_display_name(db),
    }


@app.get("/checkin/{token}")
def member_checkin_page(token: str, db: Session = Depends(get_db)):
    """Public member page behind the wall QR. No owner login required.

    Unknown token -> a friendly member-facing 404 in the app theme (never
    the owner app). Valid token -> the check-in page with this gym's name.
    """
    if not checkin_token_matches(db, token):
        page = FRONTEND_DIR / "checkin-404.html"
        if page.exists():
            return FileResponse(page, status_code=404, media_type="text/html")
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    html = (FRONTEND_DIR / "checkin.html").read_text(encoding="utf-8")
    html = html.replace("__GYM_NAME__", html_escape(gym_display_name(db)))
    html = html.replace("__CHECKIN_TOKEN__", token)
    return HTMLResponse(content=html, status_code=200)


@app.post("/api/checkin/scan")
def scan_checkin(body: ScanIn, request: Request, db: Session = Depends(get_db)):
    """Public: record a member's self check-in from the QR page.

    Idempotent per member per day: a repeat scan returns the first entry
    time instead of writing a second row. A code is only ever matched
    against THIS gym's members, and a bad code gets the same friendly
    answer regardless of where else it might exist — nothing about other
    gyms or the owner is exposed.
    """
    ip = request.client.host if request.client else "unknown"
    if scan_rate_limited(f"{ip}:{body.token}"):
        return err(429, "Too many attempts. Please wait a minute and try again.")
    if not checkin_token_matches(db, body.token):
        return err(404, "This check-in link is not valid. Please ask at the front desk for the current poster.")
    member = db.query(models.Member).filter(
        models.Member.code == body.code.strip().upper()
    ).first()
    if not member:
        return err(404, "We could not find that member code. Please check the code on your card and try again.")
    today = date.today()
    gym = gym_display_name(db)
    existing = (
        db.query(models.CheckIn)
        .filter(models.CheckIn.member_id == member.id, models.CheckIn.date == today)
        .order_by(models.CheckIn.id)
        .first()
    )
    if existing:
        return {
            "ok": True,
            "already_checked_in": True,
            "member_name": member.name,
            "member_code": member.code,
            "time": existing.time_str,
            "date": today.isoformat(),
            "gym_name": gym,
            "message": f"Already checked in at {existing.time_str}.",
        }
    now_str = datetime.now().strftime("%I:%M %p").lstrip("0")
    db.add(models.CheckIn(member_id=member.id, date=today, time_str=now_str, source="qr"))
    db.commit()
    logger.info("QR self check-in: %s (%s) at %s from %s", member.name, member.code, now_str, ip)
    return {
        "ok": True,
        "already_checked_in": False,
        "member_name": member.name,
        "member_code": member.code,
        "time": now_str,
        "date": today.isoformat(),
        "gym_name": gym,
        "message": f"{member.name} checked in at {now_str}.",
    }


# ---------------------------------------------------------------------------
# Pages (frontend + legal + custom 404)
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/privacy")
def privacy():
    return FileResponse(FRONTEND_DIR / "privacy.html")


@app.get("/pricing")
def pricing():
    return FileResponse(FRONTEND_DIR / "pricing.html")


@app.get("/terms")
def terms():
    return FileResponse(FRONTEND_DIR / "terms.html")


# Brand icon assets (public — browsers request favicons without a session).
@app.get("/icon-512.png")
def icon_512():
    return FileResponse(FRONTEND_DIR / "icon-512.png", media_type="image/png")


@app.get("/icon-192.png")
def icon_192():
    return FileResponse(FRONTEND_DIR / "icon-192.png", media_type="image/png")


@app.get("/favicon-32.png")
def favicon_32():
    return FileResponse(FRONTEND_DIR / "favicon-32.png", media_type="image/png")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api"):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    page = FRONTEND_DIR / "404.html"
    if page.exists():
        return FileResponse(page, status_code=404)
    return JSONResponse(status_code=404, content={"detail": "Not found."})


if __name__ == "__main__":
    # Local / simple-host entry point:  python3 main.py
    # Binds all interfaces; port comes from the PORT env var (default 8000).
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
