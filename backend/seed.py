"""Demo data seeding.

48 members. On first load:
- No-Show Red List (10+ days absent) = 32 members, top-6 exactly as the
  reference video: Rohit A. 16d, Sandeep K. 15d, Prakash M. 14d, Amit S. 13d,
  Vikram G. 12d, Deepak P. 11d. The remaining red-list members are 10-11 days
  absent so the video's exact top-6 order is preserved under a
  highest-days-first sort.
- 5 members already checked in today.
- 6 memberships due within 7 days, 3 expired.
All dates are computed relative to *today* at seed time, so the demo is
always correct whenever it is first run.
"""
from datetime import date, datetime, timedelta
import secrets

from sqlalchemy.orm import Session

import models
from security import hash_password

# The demo owner already has details completed and a plan chosen, so login
# goes straight to the dashboard (no forced entry flow).
DEMO_DETAILS = {
    "owner_name": "Gym Owner",
    "gym_name": "PowerHouse Gym",
    "mobile": "9876543210",
    "whatsapp": "9876543210",
    "gym_address": "MG Road, Near City Mall",
    "gym_city": "Hyderabad",
    "gym_phone": "9876543210",
    "opening_year": 2021,
}

PLANS = {
    "Monthly": {"days": 30, "price": 1000},
    "Quarterly": {"days": 90, "price": 2500},
    "Yearly": {"days": 365, "price": 9000},
}

ADDONS = [
    {"id": "pt", "name": "Personal Training", "price": 5000, "icon": "🏋️"},
    {"id": "diet", "name": "Diet Plan", "price": 1500, "icon": "🥗"},
    {"id": "steam", "name": "Steam & Sauna", "price": 999, "icon": "🧖"},
    {"id": "group", "name": "Group Classes", "price": 1999, "icon": "👥"},
    {"id": "locker", "name": "Locker", "price": 499, "icon": "🔐"},
    {"id": "protein", "name": "Protein Pack", "price": 2499, "icon": "🥤"},
]

DEMO_EMAIL = "owner@powerhousegym.in"
DEMO_PASSWORD = "Gym@2026"

# (name, plan, expiry_offset_days_from_today, last_visit_offset_days_ago)
SEED_MEMBERS = [
    # --- The video's top 6 (No-Show Red List) ---
    ("Rohit A.", "Monthly", 95, 16),
    ("Sandeep K.", "Quarterly", 140, 15),
    ("Prakash M.", "Monthly", 60, 14),
    ("Amit S.", "Yearly", 200, 13),
    ("Vikram G.", "Quarterly", 33, 12),
    ("Deepak P.", "Monthly", 120, 11),
    # --- 10 more red-list members, 11 days absent ---
    ("Arjun S.", "Yearly", 88, 11),
    ("Rahul V.", "Monthly", 41, 11),
    ("Nikhil T.", "Quarterly", 150, 11),
    ("Varun D.", "Monthly", 77, 11),
    ("Aditya R.", "Yearly", 230, 11),
    ("Imran F.", "Quarterly", 52, 11),
    ("Sahil B.", "Monthly", 66, 11),
    ("Rohan J.", "Monthly", 24, 11),
    ("Kabir H.", "Monthly", 21, 11),
    ("Manish L.", "Quarterly", 99, 11),
    # --- 16 more red-list members, 10 days absent (= 32 total) ---
    ("Akash G.", "Monthly", 38, 10),
    ("Vivek N.", "Yearly", 180, 10),
    ("Suraj P.", "Quarterly", 71, 10),
    ("Priya S.", "Monthly", 44, 10),
    ("Sneha K.", "Quarterly", 130, 10),
    ("Anjali R.", "Yearly", 260, 10),
    ("Kavita D.", "Monthly", 58, 10),
    ("Pooja M.", "Quarterly", 83, 10),
    ("Rajesh C.", "Monthly", 36, 10),
    ("Sanjay W.", "Yearly", 145, 10),
    ("Harish B.", "Quarterly", 92, 10),
    ("Nitin A.", "Monthly", 49, 10),
    ("Mohit Z.", "Quarterly", 110, 10),
    ("Tarun E.", "Monthly", 63, 10),
    ("Yogesh Q.", "Yearly", 190, 10),
    ("Sameer U.", "Quarterly", 105, 10),
    # --- 16 active members (last visit 0-9 days ago) ---
    ("Aryan K.", "Yearly", 210, 0),
    ("Ishita R.", "Quarterly", 64, 0),
    ("Advait P.", "Monthly", 18, 0),
    ("Meera S.", "Yearly", 320, 0),
    ("Arnav D.", "Quarterly", 45, 0),
    ("Zara F.", "Quarterly", 2, 1),       # renewal due in 2 days
    ("Vivaan H.", "Monthly", 1, 2),       # renewal due in 1 day
    ("Ananya G.", "Monthly", 3, 2),       # due in 3 days
    ("Reyansh L.", "Yearly", 5, 3),       # due in 5 days
    ("Diya N.", "Quarterly", 6, 4),       # due in 6 days
    ("Atharv B.", "Monthly", 7, 5),       # due in 7 days
    ("Sara M.", "Monthly", -2, 5),        # expired 2 days ago
    ("Vihaan C.", "Quarterly", -4, 6),    # expired 4 days ago
    ("Ira T.", "Monthly", -6, 7),         # expired 6 days ago
    ("Ayaan W.", "Monthly", 12, 8),
    ("Kiara E.", "Quarterly", 80, 9),
]

SEED_TIMES = ["06:12 AM", "06:45 AM", "07:03 AM", "07:20 AM", "08:15 AM", "06:30 AM"]


def _phone(index: int) -> str:
    """Deterministic plausible Indian mobile (10 digits, starts with 9)."""
    return str(9000000000 + (index + 1) * 987654 + 12345)[:10]


def seed_database(db: Session) -> None:
    """(Re)create the DEMO gym's data. Tenant-scoped (2026-10-24):
    wipes only the demo owner's members, check-ins, renewals, billing
    rows and the settings the seed manages — every other gym's data
    and keys are left exactly as they were."""
    # Demo owner account (kept across resets; created first so the
    # scoped wipe knows whose rows to remove).
    user = db.query(models.User).filter(models.User.email == DEMO_EMAIL).first()
    if not user:
        user = models.User(email=DEMO_EMAIL, password_hash=hash_password(DEMO_PASSWORD))
        db.add(user)
        db.commit()
        db.refresh(user)

    demo_member_ids = [
        mid for (mid,) in db.query(models.Member.id).filter(
            (models.Member.user_id == user.id) | (models.Member.user_id.is_(None))
        ).all()
    ]
    if demo_member_ids:
        for model in (models.CheckIn, models.AddonSub, models.ManualFlag, models.ManualClear):
            db.query(model).filter(model.member_id.in_(demo_member_ids)).delete(synchronize_session=False)
        db.query(models.Renewal).filter(models.Renewal.member_id.in_(demo_member_ids)).delete(synchronize_session=False)
        db.query(models.Member).filter(models.Member.id.in_(demo_member_ids)).delete(synchronize_session=False)
    db.query(models.Renewal).filter(models.Renewal.user_id == user.id).delete(synchronize_session=False)
    db.query(models.SubscriptionPayment).filter(
        (models.SubscriptionPayment.user_id == user.id)
        | (models.SubscriptionPayment.user_id.is_(None))
    ).delete(synchronize_session=False)
    managed_keys = [
        "mode", "plan_id", "plan_period", "plan_status", "plan_since",
        "payment_status", "payment_method", "payment_amount",
        "checkin_token", "member_code_seq",
        f"checkin_token:{user.id}", f"member_code_seq:{user.id}",
    ]
    db.query(models.Setting).filter(models.Setting.key.in_(managed_keys)).delete(synchronize_session=False)
    db.commit()

    user.password_hash = hash_password(DEMO_PASSWORD)
    for key, value in DEMO_DETAILS.items():
        setattr(user, key, value)
    user.details_completed = True
    user.plan_chosen = True
    db.commit()

    today = date.today()
    members = []
    for i, (name, plan, expiry_off, visit_off) in enumerate(SEED_MEMBERS):
        m = models.Member(
            user_id=user.id,
            code=f"PHG-{i + 1:03d}",
            name=name,
            phone=_phone(i),
            plan=plan,
            expiry_date=today + timedelta(days=expiry_off),
        )
        db.add(m)
        members.append((m, visit_off))
    db.commit()

    # One historical check-in per member at their last-visit date; the five
    # "0 days" members therefore count as checked in today.
    for idx, (m, visit_off) in enumerate(members):
        db.add(models.CheckIn(
            member_id=m.id,
            date=today - timedelta(days=visit_off),
            time_str=SEED_TIMES[idx % len(SEED_TIMES)],
        ))
    db.commit()

    by_name = {m.name: m for m, _ in members}

    # Seed add-on subscriptions: revenue = 5000 + 1500 + 1500 + 499 = ₹8,499/mo
    addon_by_id = {a["id"]: a for a in ADDONS}
    for member_name, addon_id in [
        ("Aryan K.", "pt"),
        ("Ishita R.", "diet"),
        ("Diya N.", "diet"),
        ("Rohit A.", "locker"),
    ]:
        a = addon_by_id[addon_id]
        db.add(models.AddonSub(
            member_id=by_name[member_name].id,
            addon_id=a["id"],
            addon_name=a["name"],
            price=a["price"],
        ))
    db.commit()

    # Seed renewal history (both this month): ₹2,000 renewed this month.
    db.add(models.Renewal(
        user_id=user.id,
        member_id=by_name["Rohan J."].id, member_name="Rohan J.", plan="Monthly",
        amount=1000, date=today - timedelta(days=4), new_expiry=today + timedelta(days=24),
    ))
    db.add(models.Renewal(
        user_id=user.id,
        member_id=by_name["Kabir H."].id, member_name="Kabir H.", plan="Monthly",
        amount=1000, date=today - timedelta(days=9), new_expiry=today + timedelta(days=21),
    ))
    db.commit()

    # Default mode: AUTO. Default subscription: Pro (monthly), demo billing.
    db.add(models.Setting(key="mode", value="auto"))
    db.add(models.Setting(key="plan_id", value="pro"))
    db.add(models.Setting(key="plan_period", value="monthly"))
    db.add(models.Setting(key="plan_status", value="active (demo billing)"))
    db.add(models.Setting(key="plan_since", value=today.isoformat()))
    db.add(models.Setting(key="payment_status", value="active"))
    db.add(models.Setting(key="payment_method", value="upi_qr"))
    db.add(models.Setting(key="payment_amount", value="1899"))
    # The demo gym's subscription is backed by a real PAID payment record
    # (Premium monthly, billing.py price) — the pay-first gate reads the
    # latest payment row, so the demo owner is ACTIVE like any payer.
    db.add(models.SubscriptionPayment(
        user_id=user.id,
        plan_id="pro",
        plan_name="Premium",
        period="monthly",
        amount=1899,
        method="upi_qr",
        status="paid",
        created_at=datetime.now(),
    ))
    # QR self check-in: a fresh token for the DEMO gym's wall poster,
    # and its member-code counter starting above the seeded codes.
    db.add(models.Setting(key=f"checkin_token:{user.id}", value=secrets.token_urlsafe(16)))
    db.add(models.Setting(key=f"member_code_seq:{user.id}", value=str(len(SEED_MEMBERS))))
    db.commit()
    meta = db.query(models.Meta).filter(models.Meta.key == "seeded").first()
    if not meta:
        db.add(models.Meta(key="seeded", value="1", seeded=True))
    else:
        meta.value = "1"
        meta.seeded = True
    db.commit()


def seed_if_empty(db: Session) -> None:
    if db.query(models.Member).count() == 0:
        seed_database(db)
