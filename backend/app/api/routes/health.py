"""Authenticated dependency health endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from backend.app.core.security import require_roles
from backend.app.schemas import HealthResponse, UserIdentity

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health(
    request: Request,
    _: Annotated[UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))],
) -> HealthResponse:
    state = request.app.state.degradation_state
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - health must report rather than raise dependency errors
        state.mark_down("postgres", f"{type(exc).__name__}: {exc}"[:500])
    else:
        state.mark_healthy("postgres")
    await request.app.state.cache.ping()
    snapshot = state.snapshot()
    statuses = [item["status"] for item in snapshot.values()]
    overall = "down" if "down" in statuses else "degraded" if "degraded" in statuses else "healthy"
    return HealthResponse(
        status=overall,
        dependencies=snapshot,
        service=request.app.state.transaction_service.stats(),
    )
