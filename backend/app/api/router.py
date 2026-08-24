"""Authenticated API router composition and token endpoint."""

from fastapi import APIRouter, Request

from backend.app.api.routes import (
    decisions,
    feedback,
    health,
    incidents,
    metrics,
    models,
    transactions,
)
from backend.app.core.runtime import AppError
from backend.app.core.security import authenticate_user, create_access_token
from backend.app.schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/api")


@router.post("/auth/token", response_model=TokenResponse, tags=["auth"])
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    user = await authenticate_user(request, body.username, body.password)
    if user is None:
        raise AppError("invalid_credentials", 401, "Username or password is incorrect")
    settings = request.app.state.settings
    return TokenResponse(
        access_token=create_access_token(user, settings),
        expires_in_seconds=settings.jwt_expiry_minutes * 60,
        role=user.role,
    )


router.include_router(health.router)
router.include_router(transactions.router)
router.include_router(incidents.router)
router.include_router(decisions.router)
router.include_router(feedback.router)
router.include_router(metrics.router)
router.include_router(models.router)
