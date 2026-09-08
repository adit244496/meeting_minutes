"""Password hashing and JWT issue/verify.

Uses `bcrypt` directly rather than passlib - passlib 1.7.x and bcrypt 4.x have a
long-standing version-detection incompatibility that produces noisy warnings and
occasional hard failures.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])


# ---------------------------------------------------------------------------
# Short-lived resource tokens
# ---------------------------------------------------------------------------
# <audio src> and EventSource cannot send an Authorization header, so those two
# endpoints take a signed token in the query string instead. Scoping the token
# to one resource id and a short expiry means a leaked URL grants one file for
# a few minutes rather than the bearer's whole session.


def sign_resource(resource_id: str, ttl_seconds: int = 900) -> str:
    expires = int(time.time()) + ttl_seconds
    payload = f"{resource_id}:{expires}"
    digest = hmac.new(
        settings.secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{expires}.{digest}"


def verify_resource(resource_id: str, token: str) -> bool:
    try:
        raw_expires, digest = token.split(".", 1)
        expires = int(raw_expires)
    except (ValueError, AttributeError):
        return False

    if expires < time.time():
        return False

    expected = hmac.new(
        settings.secret_key.encode(), f"{resource_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, digest)
