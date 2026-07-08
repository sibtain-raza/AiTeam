"""Shared FastAPI dependencies: DB session (see db.get_db), current user."""

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .auth import decode_access_token
from .db import get_db
from .models import User

_bearer = HTTPBearer()


def _load_user(user_id: str | None, db: Session) -> User:
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    return _load_user(decode_access_token(credentials.credentials), db)


def get_current_user_sse(token: str = Query(...), db: Session = Depends(get_db)) -> User:
    """Same auth, but reads the token from a query param instead of the
    Authorization header — the browser's native EventSource API cannot set
    custom headers, so SSE endpoints conventionally accept the token this
    way. Tradeoff, not free: a URL-borne token can end up in server access
    logs or browser history. Acceptable for Phase 1 (see README); a
    hardened deployment would mint a short-lived, single-use SSE token
    instead of reusing the long-lived access token here.
    """
    return _load_user(decode_access_token(token), db)
