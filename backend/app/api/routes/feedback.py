"""Authenticated analyst outcome API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from backend.app.core.security import require_roles
from backend.app.schemas import OutcomeRequest, UserIdentity

router = APIRouter(prefix="/feedback", tags=["feedback"])
Analyst = Annotated[
    UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))
]


@router.post("/{decision_id}/outcome")
async def record_outcome(
    decision_id: str,
    body: OutcomeRequest,
    request: Request,
    actor: Analyst,
) -> dict[str, Any]:
    return await request.app.state.feedback_service.record_outcome(
        decision_id, body, actor
    )
