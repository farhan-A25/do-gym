"""Member PHG codes + the gym's public check-in token.

Two small but load-bearing pieces of state, both kept in the settings
table so they survive restarts and ride along with the daily DB backup:

* **Member codes** — every member owns a unique PHG code (PHG-001,
  PHG-002, ...) which is their identity at the door (typed once on the
  check-in page, scanned-in thereafter). Numbers come from a MONOTONIC
  counter (`member_code_seq`): the counter only ever increases, so a
  code is never reissued — even after the member holding it is deleted.
* **Check-in token** — the unguessable secret inside the gym's public
  /checkin/<token> URL, printed on the wall QR poster. Whoever holds a
  valid link can check in members of this gym (that is the point of the
  poster); resetting the token retires every old poster instantly.
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


def format_member_code(n: int) -> str:
    return f"PHG-{n:03d}"


def _max_existing_seq(db: Session) -> int:
    """Highest number among the PHG codes members currently hold."""
    best = 0
    for (code,) in db.query(models.Member.code).all():
        match = _CODE_RE.match(code or "")
        if match:
            best = max(best, int(match.group(1)))
    return best


def _current_seq(db: Session) -> int:
    """The counter value: stored counter, but never below what exists
    (bootstraps itself from seeded/imported data on first use)."""
    try:
        stored = int(_get_setting(db, CODE_SEQ_KEY, "0") or 0)
    except ValueError:
        stored = 0
    return max(stored, _max_existing_seq(db))


def next_member_code(db: Session) -> str:
    """The next PHG code, advancing and persisting the monotonic counter.

    The caller commits. Because the counter only moves forward, a code
    once issued is never handed out again — deletions cannot cause a
    collision.
    """
    n = _current_seq(db)
    while True:
        n += 1
        code = format_member_code(n)
        if not db.query(models.Member).filter(models.Member.code == code).first():
            break
    _set_setting(db, CODE_SEQ_KEY, str(n))
    return code


def ensure_member_codes(db: Session) -> int:
    """Backfill: every member must hold a PHG code.

    Hands the next sequential code to any member whose code is missing
    (NULL/empty — e.g. rows created outside the app) and persists the
    counter. Returns how many members were given a code.
    """
    missing = (
        db.query(models.Member)
        .filter((models.Member.code.is_(None)) | (models.Member.code == ""))
        .order_by(models.Member.id)
        .all()
    )
    for member in missing:
        member.code = next_member_code(db)
    if not missing:
        # Keep the stored counter bootstrapped even when nothing was fixed.
        _set_setting(db, CODE_SEQ_KEY, str(_current_seq(db)))
    db.commit()
    return len(missing)


def get_or_create_checkin_token(db: Session) -> str:
    """The gym's check-in token, generated on first use."""
    token = _get_setting(db, CHECKIN_TOKEN_KEY, "")
    if not token:
        token = secrets.token_urlsafe(16)
        _set_setting(db, CHECKIN_TOKEN_KEY, token)
        db.commit()
    return token


def reset_checkin_token(db: Session) -> str:
    """A fresh token: every previously printed QR poster stops working."""
    token = secrets.token_urlsafe(16)
    _set_setting(db, CHECKIN_TOKEN_KEY, token)
    db.commit()
    return token


def checkin_token_matches(db: Session, token: str) -> bool:
    stored = _get_setting(db, CHECKIN_TOKEN_KEY, "")
    return bool(stored) and bool(token) and secrets.compare_digest(stored, token)
