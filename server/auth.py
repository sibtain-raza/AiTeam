"""Password hashing + JWT issuance/verification.

Phase 1 simplification (see README "Honest limitations"): no refresh-token
rotation, no revocation list — a single long-lived access token. Fine for
a small number of trusted users; revisit before opening this up broadly.
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

_DEFAULT_SECRET = "dev-secret-change-me"
SECRET_KEY = os.environ.get("AITEAM_JWT_SECRET", _DEFAULT_SECRET)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week

if SECRET_KEY == _DEFAULT_SECRET:
    print(
        "WARNING: AITEAM_JWT_SECRET is not set — using an insecure default. "
        "Set a real secret before deploying this anywhere but localhost."
    )

# Using bcrypt directly rather than passlib: passlib 1.7.4 (unmaintained)
# has a known incompatibility with bcrypt>=4.1's stricter 72-byte-secret
# handling (it raises instead of the old silent-truncate behavior),
# confirmed live as a ValueError on signup. bcrypt's own limit is applied
# explicitly below instead of relying on a compatibility shim.


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None
