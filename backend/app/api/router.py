"""Authenticated API router composition and token endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from backend.app.api.routes import (
    decisions,
    demo,
    feedback,
    health,
    incidents,
    metrics,
    models,
    transactions,
)
from backend.app.core.runtime import AppError
from backend.app.core.security import (
    authenticate_user,
    create_access_token,
    require_local_request,
)
from backend.app.schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/api")
LocalRequest = Annotated[None, Depends(require_local_request)]


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


@router.get("/auth/demo-credentials", tags=["auth"])
async def demo_credentials(
    request: Request,
    response: Response,
    _local: LocalRequest,
) -> dict:
    """Return seeded operator logins only to a direct local demo client.

    Values live only in the operator's .env, never in committed source.
    """

    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    return {
        "environment": settings.app_env,
        "roles": {
            "analyst": {
                "username": settings.demo_analyst_username,
                "password": settings.demo_analyst_password,
            },
            "lead_analyst": {
                "username": settings.demo_lead_analyst_username,
                "password": settings.demo_lead_analyst_password,
            },
            "admin": {
                "username": settings.demo_admin_username,
                "password": settings.demo_admin_password,
            },
        },
    }


router.include_router(health.router)
router.include_router(demo.router)
router.include_router(transactions.router)
router.include_router(incidents.router)
router.include_router(decisions.router)
router.include_router(feedback.router)
router.include_router(metrics.router)
router.include_router(models.router)
