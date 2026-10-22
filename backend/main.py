"""PowerHouse Gym — Owner App (FastAPI backend).

Run from this folder:
    pip install -r requirements.txt
    uvicorn main:app --port 8000
Then open http://localhost:8000 and log in with the demo owner account
(see README.md).
"""
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from billing import SUBSCRIPTION_PLANS, get_subscription_plan
from database import Base, SessionLocal, engine, get_db
from seed import ADDONS, PLANS, seed_database, seed_if_empty
from security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_session_token,
    hash_password,
    read_session_token,
    verify_password,
)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
STATIC_DIR = BASE_DIR / "static"
PAYMENT_QR_FILE = STATIC_DIR / "payment-qr.png"
LOG_DIR = BASE_DIR.parent / "logs"
BACKUP_DIR = BASE_DIR.parent / "backups"

# ---------------------------------------------------------------------------
# Logging: everything important goes to logs/app.log (rotating), nothing silent
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("powerhouse")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)


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
SEED_ENABLED = os.environ.get("SEED_DEMO", "true").strip().lower() != "false"
with SessionLocal() as _db:
    if SEED_ENABLED:
        seed_if_empty(_db)
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
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
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
    return response


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
# Schemas (Pydantic validation on every endpoint)
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class CheckInIn(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    member_id: int | None = None


class RenewIn(BaseModel):
    custom_days: int | None = Field(default=None, ge=1, le=3650)


class AssignIn(BaseModel):
    member_id: int
    addon_id: str = Field(min_length=1, max_length=32)


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
def current_user(request: Request, db: Session = Depends(get_db)):
    """Return the logged-in User, or a 401 JSONResponse (endpoints check for it)."""
    payload = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not payload:
        return _unauthorized()
    user = db.query(models.User).filter(models.User.id == payload.get("uid")).first()
    if not user:
        return _unauthorized()
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
    subs = db.query(models.AddonSub).filter(models.AddonSub.member_id == m.id).all()
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
        "addons": [
            {"addon_id": s.addon_id, "addon_name": s.addon_name, "price": s.price}
            for s in subs
        ],
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


def get_plan_state(db: Session) -> dict:
    """The gym's current subscription (demo owner starts on Premium, monthly).

    Price and payment fields are computed here, server-side, from
    billing.py and the recorded subscription payments — the client never
    supplies an amount.
    """
    plan_id = get_setting(db, "plan_id", "pro")
    plan = get_subscription_plan(plan_id) or get_subscription_plan("pro")
    period = get_setting(db, "plan_period", "monthly")
    members_used = db.query(models.Member).count()
    recent = (
        db.query(models.SubscriptionPayment)
        .order_by(models.SubscriptionPayment.id.desc())
        .limit(5)
        .all()
    )
    last_payment = payment_dict(recent[0]) if recent else None
    payment_status = get_setting(db, "payment_status", "")
    return {
        "plan_id": plan["id"],
        "period": period,
        "status": get_setting(db, "plan_status", "active (demo billing)"),
        "since": get_setting(db, "plan_since", ""),
        "plan": plan,
        "price": plan["yearly"] if period == "yearly" else plan["monthly"],
        "payment_status": payment_status or None,  # active | payment_pending | None
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
def _session_response(request: Request, user: models.User) -> JSONResponse:
    """JSON response that also sets the signed, HTTP-only session cookie."""
    token = create_session_token(user.id, user.email)
    # Secure cookie: ON automatically when the request arrived over HTTPS
    # (directly or via a proxy's X-Forwarded-Proto), or when forced by env.
    behind_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        or os.environ.get("COOKIE_SECURE", "") == "1"
    )
    resp = JSONResponse(content={"ok": True, **user_payload(user)})
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax", path="/",
        secure=behind_https,
    )
    return resp


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
        details_completed=False,
        plan_chosen=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("New gym signed up: %s (%s)", email, gym_name)
    return _session_response(request, user)
@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited(ip):
        return err(429, "Too many login attempts. Please wait a minute and try again.")
    user = db.query(models.User).filter(models.User.email == body.email.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        return err(401, "Wrong email or password. Please try again.")
    logger.info("Login success for %s from %s", user.email, ip)
    return _session_response(request, user)


@app.post("/api/logout")
def logout():
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    return user_payload(user)


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
    addon_revenue = sum(s.price for s in db.query(models.AddonSub).all())
    return {
        "total_members": len(members),
        "checked_in_today": len({c.member_id for c in today_checkins}),
        "red_list_count": len(red_list(db, today)),
        "renewals_due": len(due),
        "expired_count": len(expired),
        "addon_revenue_month": addon_revenue,
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
    db.add(models.CheckIn(member_id=member.id, date=today, time_str=now_str))
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
            out.append({"member_id": m.id, "name": m.name, "code": m.code, "time": c.time_str})
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


@app.get("/api/addons")
def addons(db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    subs = db.query(models.AddonSub).all()
    catalog = []
    for a in ADDONS:
        catalog.append({**a, "member_count": sum(1 for s in subs if s.addon_id == a["id"])})
    active = []
    for s in subs:
        m = db.query(models.Member).filter(models.Member.id == s.member_id).first()
        active.append({
            "sub_id": s.id, "member_id": s.member_id,
            "member_name": m.name if m else "Unknown",
            "addon_id": s.addon_id, "addon_name": s.addon_name, "price": s.price,
        })
    return {
        "catalog": catalog,
        "active": active,
        "revenue_month": sum(s.price for s in subs),
    }


@app.post("/api/addons/assign")
def assign_addon(body: AssignIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    addon = next((a for a in ADDONS if a["id"] == body.addon_id), None)
    if not addon:
        return err(404, "Add-on not found.")
    m = db.query(models.Member).filter(models.Member.id == body.member_id).first()
    if not m:
        return err(404, "Member not found.")
    existing = db.query(models.AddonSub).filter(
        models.AddonSub.member_id == m.id, models.AddonSub.addon_id == addon["id"]
    ).first()
    if existing:
        return err(400, f"{m.name} already has {addon['name']}.")
    db.add(models.AddonSub(member_id=m.id, addon_id=addon["id"], addon_name=addon["name"], price=addon["price"]))
    db.commit()
    return {"ok": True, "message": f"{addon['name']} assigned to {m.name}."}


@app.delete("/api/addons/subs/{sub_id}")
def remove_addon(sub_id: int, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    sub = db.query(models.AddonSub).filter(models.AddonSub.id == sub_id).first()
    if not sub:
        return err(404, "Subscription not found.")
    db.delete(sub)
    db.commit()
    return {"ok": True}


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
    return get_plan_state(db)


@app.post("/api/plan/switch")
def switch_plan(body: PlanSwitchIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    if isinstance(user, JSONResponse):
        return user
    plan = get_subscription_plan(body.plan_id)
    if not plan:
        return err(404, "Plan not found.")
    today = date.today()
    set_setting(db, "plan_id", body.plan_id)
    set_setting(db, "plan_period", body.period)
    set_setting(db, "plan_status", "active (demo billing)")
    if not get_setting(db, "plan_since"):
        set_setting(db, "plan_since", today.isoformat())
    db.commit()
    # Audit: every plan change is written to the server log.
    logger.info(
        "Plan switched by %s -> %s (%s), status: active (demo billing)",
        user.email, body.plan_id, body.period,
    )
    user.plan_chosen = True
    db.commit()
    state = get_plan_state(db)
    state["message"] = f"You are now on the {plan['name']} plan ({body.period})."
    return state


# ---------------------------------------------------------------------------
# Subscription payment by UPI QR ("I Have Paid" / "Pay Later")
# ---------------------------------------------------------------------------
def _record_subscription_payment(db: Session, user: models.User, body: SubscriptionPayIn, paid: bool):
    """Shared logic for both payment buttons.

    The amount ALWAYS comes from billing.py (server-side) — the request
    only carries plan + period. Writes a SubscriptionPayment row, updates
    the subscription settings, marks the plan as chosen (entry flow) and
    audit-logs the event.
    """
    plan = get_subscription_plan(body.plan_id)
    if not plan:
        return None
    amount = plan["monthly"] if body.period == "monthly" else plan["yearly"]
    db.add(models.SubscriptionPayment(
        user_id=user.id,
        plan_id=plan["id"],
        plan_name=plan["name"],
        period=body.period,
        amount=amount,
        method="upi_qr",
        status="paid" if paid else "pending",
        created_at=datetime.now(),
    ))
    today = date.today()
    set_setting(db, "plan_id", plan["id"])
    set_setting(db, "plan_period", body.period)
    set_setting(db, "plan_status", "active" if paid else "payment_pending")
    set_setting(db, "payment_status", "active" if paid else "payment_pending")
    set_setting(db, "payment_method", "upi_qr")
    set_setting(db, "payment_amount", str(amount))
    if paid or not get_setting(db, "plan_since"):
        set_setting(db, "plan_since", today.isoformat())
    user.plan_chosen = True
    db.commit()
    logger.info(
        "Subscription payment %s by %s -> %s (%s), amount ₹%d via UPI QR",
        "PAID" if paid else "PENDING (pay later)", user.email,
        plan["id"], body.period, amount,
    )
    state = get_plan_state(db)
    if paid:
        state["message"] = (
            f"Payment recorded — your {plan['name']} plan ({body.period}) is active. "
            "Thank you!"
        )
    else:
        state["message"] = (
            f"Your {plan['name']} plan ({body.period}) is saved with payment pending. "
            "You can pay anytime from Plan & Billing."
        )
    return state


@app.post("/api/subscription/confirm-payment")
def confirm_payment(body: SubscriptionPayIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """"I Have Paid": activate the chosen plan; amount from billing.py."""
    if isinstance(user, JSONResponse):
        return user
    state = _record_subscription_payment(db, user, body, paid=True)
    if state is None:
        return err(404, "Plan not found.")
    return state


@app.post("/api/subscription/pay-later")
def pay_later(body: SubscriptionPayIn, db: Session = Depends(get_db), user: models.User = Depends(current_user)):
    """"Pay Later": save the plan with payment pending; the app stays usable."""
    if isinstance(user, JSONResponse):
        return user
    state = _record_subscription_payment(db, user, body, paid=False)
    if state is None:
        return err(404, "Plan not found.")
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
