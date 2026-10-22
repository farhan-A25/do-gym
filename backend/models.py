"""SQLAlchemy models for the PowerHouse Gym Owner App."""
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String
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


class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, nullable=False, index=True)  # e.g. PHG-001
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
    """One subscription payment event for using this app (UPI QR flow).

    A row is written when the owner taps "I Have Paid" (status "paid") or
    "Pay Later" (status "pending"). Amounts are always computed server-side
    from billing.py — never taken from the client.
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
