"""Demo-grade JWT, password, service-token, and role authorization."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from backend.app.config import Settings, get_settings
from backend.app.core.runtime import AppError
from backend.app.db.models import User
from backend.app.schemas import UserIdentity

_bearer = HTTPBearer(auto_error=False)
LOCAL_DEMO_ENVS = frozenset({"development", "local", "test", "testing"})
_TEST_CLIENT_HOSTS = frozenset({"testclient"})


def require_local_request(request: Request) -> None:
    """Allow demo-only HTTP surfaces only from the direct loopback peer."""

    environment = request.app.state.settings.app_env.lower()
    if environment not in LOCAL_DEMO_ENVS:
        raise AppError("not_found", 404, "Not found")

    host = request.client.host if request.client is not None else None
    if environment in {"test", "testing"} and host in _TEST_CLIENT_HOSTS:
        return
    try:
        address = ip_address(host) if host is not None else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise AppError("not_found", 404, "Not found")


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) < 8 or len(encoded) > 72:
        raise ValueError("Passwords must contain 8-72 UTF-8 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, UnicodeError):
        return False


def create_access_token(user: UserIdentity, settings: Settings | None = None) -> str:
    config = settings or get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=config.jwt_expiry_minutes),
    }
    return jwt.encode(payload, config.jwt_secret_key, algorithm=config.jwt_algorithm)


def decode_access_token(token: str, settings: Settings | None = None) -> UserIdentity:
    config = settings or get_settings()
    try:
        payload = jwt.decode(token, config.jwt_secret_key, algorithms=[config.jwt_algorithm])
        return UserIdentity(username=payload["sub"], role=payload["role"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AppError("invalid_token", 401, "Authentication token is invalid or expired") from exc


async def authenticate_user(request: Request, username: str, password: str) -> UserIdentity | None:
    async with request.app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return None
    return UserIdentity(username=user.username, role=user.role)


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UserIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError("authentication_required", 401, "Bearer authentication is required")
    identity = decode_access_token(credentials.credentials, request.app.state.settings)
    async with request.app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.username == identity.username))
    if user is None or not user.is_active or user.role != identity.role:
        raise AppError("authentication_required", 401, "User is inactive or unavailable")
    return identity


def require_roles(*roles: str):
    async def dependency(user: Annotated[UserIdentity, Depends(current_user)]) -> UserIdentity:
        if user.role not in roles:
            raise AppError("forbidden", 403, "This role cannot perform the requested action")
        return user

    return dependency


async def require_service_token(
    request: Request,
    x_service_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.service_token
    if x_service_token is None or not hmac.compare_digest(x_service_token, expected):
        raise AppError("invalid_service_token", 401, "A valid service token is required")
