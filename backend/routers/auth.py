"""
routers/auth.py
Registration and login endpoints for user-side access.
"""

from fastapi import APIRouter, HTTPException, Depends
from psycopg2 import IntegrityError

import database as db
from auth_service import hash_password, verify_password, create_auth_token, get_current_user
from schemas import RegisterUserRequest, LoginRequest, AuthResponse, AuthUserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _serialize_user(user: dict) -> AuthUserResponse:
    return AuthUserResponse(
        id=user["id"],
        name=user["name"],
        email=user["email"],
        role=user["role"],
    )


@router.post("/register", response_model=AuthResponse)
def register(body: RegisterUserRequest):
    if len(body.password) < 6:
        raise HTTPException(400, detail="Password must be at least 6 characters long")

    try:
        user = db.create_user(
            name=body.name.strip(),
            email=body.email.strip(),
            password_hash=hash_password(body.password),
            role="user",
        )
    except IntegrityError:
        raise HTTPException(409, detail="An account with this email already exists")

    token = create_auth_token(user["id"])
    return AuthResponse(token=token, user=_serialize_user(user))


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest):
    user = db.get_user_by_email(body.email.strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, detail="Invalid email or password")

    token = create_auth_token(user["id"])
    return AuthResponse(token=token, user=_serialize_user(user))


@router.get("/me", response_model=AuthUserResponse)
def me(user=Depends(get_current_user)):
    return _serialize_user(user)
