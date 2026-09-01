"""
auth_service.py
Authentication helpers for password hashing and bearer-token auth.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import database as db
from config import settings

TOKEN_TTL_DAYS = 7
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, expected = password_hash.split("$", 1)
    except ValueError:
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()
    return hmac.compare_digest(actual, expected)


def create_auth_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    db.create_auth_token(
        token=token,
        user_id=user_id,
        expires_in=timedelta(days=TOKEN_TTL_DAYS),
    )
    return token


def _get_user_from_credentials(credentials: HTTPAuthorizationCredentials | None):
    if not credentials or not credentials.credentials:
        raise HTTPException(401, detail="Authentication required")

    user = db.get_user_by_token(credentials.credentials)
    if not user:
        raise HTTPException(401, detail="Invalid or expired token")
    if not user.get("is_active", True):
        raise HTTPException(403, detail="User account is inactive")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    return _get_user_from_credentials(credentials)


def require_role(*allowed_roles: str):
    def dependency(user=Depends(get_current_user)):
        if user["role"] not in allowed_roles:
            raise HTTPException(403, detail="You do not have access to this resource")
        return user

    return dependency


def ensure_default_admin():
    return db.upsert_user_by_email(
        name=settings.admin_name,
        email=settings.admin_email,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )