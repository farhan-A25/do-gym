"""Password hashing (bcrypt) and signed session-cookie helpers (itsdangerous)."""
import os

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-in-production")
SESSION_COOKIE = "phg_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days, in seconds

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="phg-session-v1")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_session_token(user_id: int, email: str) -> str:
    return _serializer.dumps({"uid": user_id, "email": email})


def read_session_token(token: str):
    """Return the session payload dict, or None if invalid/expired."""
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
