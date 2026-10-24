"""Member PHG codes + each gym's public check-in token.

Tenant isolation (2026-10-24): codes and tokens are PER OWNER. Every
gym runs its own monotonic PHG sequence (PHG-001, PHG-002, ...) and
holds its own wall-QR token. State lives in the settings table under
owner-scoped keys so it survives restarts and rides along with the
daily DB backup:

* ``member_code_seq:<user_id>`` — that gym's PHG counter. The counter
  only ever increases, so a code is never reissued inside a gym — even
  after the member holding it is deleted.
* ``checkin_token:<user_id>`` — that gym's unguessable secret inside
  its public /checkin/<token> URL, printed on its wall QR poster.
  Resetting a gym's token retires that gym's old posters only.

The un-suffixed legacy keys (``member_code_seq`` / ``checkin_token``)
belong to databases created before tenant isolation; they are treated
as the first (demo) owner's, so old posters and counters keep working.
"""
import re
import secrets

from sqlalchemy.orm import Session

import models

CODE_SEQ_KEY = "member_code_seq"
CHECKIN_TOKEN_KEY = "checkin_token"
_CODE_RE = re.compile(r"^PHG-(\d+)$")


def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    return row.value if row else default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if not row:
        db.add(models.Setting(key=key, value=value))
    else:
        row.value = value


def _seq_key(user_id: int | None) -> str:
    return CODE_SEQ_KEY if user_id is None else f"{CODE_SEQ_KEY}:{user_id}"


def _token_key(user_id: int | None) -> str:
    return CHECKIN_TOKEN_KEY if user_id is None else f"{CHECKIN_TOKEN_KEY}:{user_id}"


def format_member_code(n: int) -> str:
    return f"PHG-{n:03d}"


def _max_existing_seq(db: Session, user_id: int | None) -> int:
    """Highest number among the PHG codes THIS gym's members hold."""
    best = 0
    q = db.query(models.Member.code)
    if user_id is not None:
        q = q.filter(models.Member.user_id == user_id)
    for (code,) in q.all():
        match = _CODE_RE.match(code or "")
        if match:
            best = max(best, int(match.group(1)))
    return best


def _current_seq(db: Session, user_id: int | None) -> int:
    """The counter value: stored counter, but never below what exists
    (bootstraps itself from seeded/imported data on first use)."""
    try:
        stored = int(_get_setting(db, _seq_key(user_id), "0") or 0)
    except ValueError:
        stored = 0
    return max(stored, _max_existing_seq(db, user_id))


def next_member_code(db: Session, user_id: int | None) -> str:
    """The next PHG code for THIS gym, advancing its monotonic counter.

    The caller commits. Because the counter only moves forward, a code
    once issued in a gym is never handed out again there — deletions
    cannot cause a collision.
    """
    n = _current_seq(db, user_id)
    while True:
        n += 1
        code = format_member_code(n)
        q = db.query(models.Member).filter(models.Member.code == code)
        if user_id is not None:
            q = q.filter(models.Member.user_id == user_id)
        if not q.first():
            break
    _set_setting(db, _seq_key(user_id), str(n))
    return code


def ensure_member_codes(db: Session) -> int:
    """Backfill: every member must hold a PHG code, per owning gym.

    Hands the next sequential code (in that gym's own sequence) to any
    member whose code is missing (NULL/empty — e.g. rows created
    outside the app) and persists the counters. Returns how many
    members were given a code.
    """
    missing = (
        db.query(models.Member)
        .filter((models.Member.code.is_(None)) | (models.Member.code == ""))
        .order_by(models.Member.id)
        .all()
    )
    for member in missing:
        member.code = next_member_code(db, member.user_id)
    if missing:
        touched_owners = {m.user_id for m in missing}
    else:
        # Keep every gym's stored counter bootstrapped even when
        # nothing was fixed.
        touched_owners = {
            uid for (uid,) in db.query(models.Member.user_id).distinct().all()
        }
    for uid in touched_owners:
        _set_setting(db, _seq_key(uid), str(_current_seq(db, uid)))
    db.commit()
    return len(missing)


def get_or_create_checkin_token(db: Session, user_id: int | None) -> str:
    """This gym's check-in token, generated on first use. The FIRST
    owner inherits the legacy global token (pre-isolation databases)
    so an already printed poster keeps working; every other gym gets
    its own fresh token."""
    token = _get_setting(db, _token_key(user_id), "")
    if not token and user_id is not None:
        first = db.query(models.User).order_by(models.User.id).first()
        if first is not None and first.id == user_id:
            token = _get_setting(db, CHECKIN_TOKEN_KEY, "")
    if not token:
        token = secrets.token_urlsafe(16)
        _set_setting(db, _token_key(user_id), token)
        db.commit()
    return token


def reset_checkin_token(db: Session, user_id: int | None) -> str:
    """A fresh token for THIS gym: its previously printed QR posters
    stop working; other gyms' posters are untouched."""
    token = secrets.token_urlsafe(16)
    _set_setting(db, _token_key(user_id), token)
    db.commit()
    return token


def checkin_token_owner(db: Session, token: str) -> int | None:
    """Resolve a public check-in token to its owning gym (user id).

    Per-owner tokens map directly; the legacy global token maps to the
    first owner (the demo gym it was created for). Returns None when
    the token matches no gym.
    """
    if not token:
        return None
    rows = (
        db.query(models.Setting)
        .filter(models.Setting.key.like(f"{CHECKIN_TOKEN_KEY}%"))
        .all()
    )
    legacy_owner: int | None = None
    for row in rows:
        if not (row.value and secrets.compare_digest(row.value, token)):
            continue
        if row.key == CHECKIN_TOKEN_KEY:
            first = db.query(models.User).order_by(models.User.id).first()
            legacy_owner = first.id if first else None
        elif row.key.startswith(f"{CHECKIN_TOKEN_KEY}:"):
            try:
                return int(row.key.split(":", 1)[1])
            except ValueError:
                continue
    return legacy_owner


def checkin_token_matches(db: Session, token: str) -> bool:
    return checkin_token_owner(db, token) is not None
