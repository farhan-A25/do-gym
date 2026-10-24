"""SQLAlchemy models for the PowerHouse Gym Owner App."""
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    # Entry-flow + gym/owner details (collected on the Details page)
    owner_name = Column(String(120), default="")
    gym_name = Column(String(160), default="")
    mobile = Column(String(20), default="")
    whatsapp = Column(String(20), default="")
    gym_address = Column(String(255), default="")
    gym_city = Column(String(80), default="")
    gym_phone = Column(String(20), default="")
    opening_year = Column(Integer, nullable=True)
    details_completed = Column(Boolean, default=False)
    plan_chosen = Column(Boolean, default=False)
    # How this account was created: "email" (password signup, the
    # default) or "google" (Continue with Google — email verified by
    # Google). Shown as a badge on the admin control page.
    auth_provider = Column(String(16), nullable=False, default="email")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Canonical signup flow (owner's order, 2026-10-23): account created
    # (Google or email) -> OTP to that email -> details -> payment.
    # While True, everything except the OTP endpoints is blocked.
    # Accounts that predate the OTP stage default to False (verified).
    otp_pending = Column(Boolean, nullable=False, default=False)
    # Session epoch: every issued session token carries it; logging
    # out bumps it, invalidating all previously issued tokens at once.
    session_epoch = Column(Integer, nullable=False, default=0)


class OtpChallenge(Base):
    """The live email-OTP challenge for one account (at most one row).

    Only the HASH of the 6-digit code is stored (plus, ONLY when the
    dev-only DEV_OTP_ECHO=1 flag is on, the plain code in dev_code so
    tests/local dev can complete the flow without an SMTP server).
    """

    __tablename__ = "otp_challenges"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    code_hash = Column(String(64), nullable=False)
    dev_code = Column(String(8), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_sent_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PasswordResetChallenge(Base):
    """The live password-reset challenge for one account (at most one
    row) — written by POST /api/auth/forgot, consumed by
    POST /api/auth/reset.

    Deliberately a SEPARATE table from OtpChallenge (the signup OTP):
    reset codes and signup codes live in different tables and are
    hashed with different domain prefixes, so a code issued for one
    purpose can never be spent on the other. Same storage rules as
    the signup OTP: only the HASH is kept (plus the plain code in
    dev_code ONLY when the dev-only DEV_OTP_ECHO=1 flag is on).
    """

    __tablename__ = "password_reset_challenges"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    code_hash = Column(String(64), nullable=False)
    dev_code = Column(String(8), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_sent_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AdminAction(Base):
    """Audit trail for Farhan's Control support actions (2026-10-23).

    One row per support action taken from /control or the admin API:
    what was done, which account it hit, and when. Deliberately stores
    NO secrets — never a password, never a code — only the fact that
    the action happened, so the trail itself can never leak a rescue
    credential.
    """

    __tablename__ = "admin_actions"

    id = Column(Integer, primary_key=True)
    action = Column(String(64), nullable=False)
    target_user_id = Column(Integer, nullable=True)
    target_email = Column(String(255), nullable=False, default="")
    detail = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Member(Base):
    __tablename__ = "members"
    # Tenant isolation (2026-10-24): every member belongs to exactly ONE
    # gym owner. PHG codes are unique per owner, not globally — each gym
    # runs its own PHG-001, PHG-002, ... sequence.
    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_member_owner_code"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code = Column(String(16), nullable=False, index=True)  # e.g. PHG-001
    name = Column(String(120), nullable=False)
    phone = Column(String(20), nullable=False)  # Indian mobile, digits only (10)
    plan = Column(String(20), nullable=False)  # Monthly | Quarterly | Yearly
    expiry_date = Column(Date, nullable=False)

    checkins = relationship("CheckIn", back_populates="member", cascade="all, delete-orphan")
    addon_subs = relationship("AddonSub", back_populates="member", cascade="all, delete-orphan")


class CheckIn(Base):
    __tablename__ = "checkins"

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    time_str = Column(String(16), nullable=False)  # display string, e.g. "07:15 AM"
    source = Column(String(16), nullable=False, default="owner")  # owner | qr

    member = relationship("Member", back_populates="checkins")


class AddonSub(Base):
    """An active add-on subscription for a member."""
    __tablename__ = "addon_subs"

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    addon_id = Column(String(32), nullable=False)
    addon_name = Column(String(80), nullable=False)
    price = Column(Integer, nullable=False)  # ₹ per month

    member = relationship("Member", back_populates="addon_subs")


class Renewal(Base):
    """Renewal history record."""
    __tablename__ = "renewals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    member_id = Column(Integer, nullable=True)
    member_name = Column(String(120), nullable=False)
    plan = Column(String(20), nullable=False)
    amount = Column(Integer, nullable=False)  # ₹
    date = Column(Date, nullable=False)
    new_expiry = Column(Date, nullable=False)


class ManualFlag(Base):
    """A member the owner manually flagged onto the No-Show Red List (Manual mode)."""
    __tablename__ = "manual_flags"

    member_id = Column(Integer, ForeignKey("members.id"), primary_key=True)


class ManualClear(Base):
    """A member the owner manually cleared from the Red List (Manual mode)."""
    __tablename__ = "manual_clears"

    member_id = Column(Integer, ForeignKey("members.id"), primary_key=True)


class SubscriptionPayment(Base):
    """One recorded subscription payment for using this app.

    A row is written when a payment is CONFIRMED — by Razorpay's
    webhook (method "razorpay") or, as a non-UI fallback, by the
    owner's manual confirm endpoint (method "upi_qr"). Rows with status
    "pending" exist only in databases created before the Pay Later
    button was removed (2026-10-23) — the pay-first gate treats them as
    UNPAID. Amounts are always computed server-side from billing.py —
    never taken from the client. The gateway ids make webhook
    deliveries idempotent: the same Razorpay payment id is never
    recorded twice.
    """
    __tablename__ = "subscription_payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    plan_id = Column(String(32), nullable=False)      # starter | pro
    plan_name = Column(String(80), nullable=False)    # Standard | Premium
    period = Column(String(16), nullable=False)       # monthly | yearly
    amount = Column(Integer, nullable=False)          # ₹, from billing.py
    method = Column(String(32), nullable=False, default="upi_qr")
    status = Column(String(16), nullable=False)       # paid | pending
    gateway_order_id = Column(String(64), nullable=True)    # Razorpay order_…
    gateway_payment_id = Column(String(64), nullable=True)  # Razorpay pay_…
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class GatewayOrder(Base):
    """One Razorpay order created for a subscription checkout.

    Written when the owner picks a plan and the server creates the
    order (the owner's user id travels in the order notes, so the
    webhook can be matched back to this gym). Status flips to "paid"
    when Razorpay's webhook confirms the money; the matching
    SubscriptionPayment row is what actually lifts the pay-first gate.
    """
    __tablename__ = "gateway_orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    payment_id = Column(String(64), nullable=True)  # filled on capture
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    plan_id = Column(String(32), nullable=False)      # starter | pro
    period = Column(String(16), nullable=False)       # monthly | yearly
    amount = Column(Integer, nullable=False)          # ₹, from billing.py
    status = Column(String(16), nullable=False, default="created")  # created | paid
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)


class Meta(Base):
    """Internal bookkeeping (e.g. seeded flag)."""
    __tablename__ = "meta"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)
    seeded = Column(Boolean, default=False)


class FaceTemplate(Base):
    """Farhan's enrolled face template for face unlock (singleton row).

    ONLY the numeric face descriptors computed in his browser during
    enrollment (a JSON list of 128-float arrays, one per pose) — never
    a photo. Re-registering from Control replaces the descriptors.
    """

    __tablename__ = "face_templates"

    id = Column(Integer, primary_key=True)
    descriptors = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
