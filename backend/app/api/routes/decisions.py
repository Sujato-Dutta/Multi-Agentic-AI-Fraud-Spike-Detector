"""Authenticated human-review and audit API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from backend.app.core.security import require_roles
from backend.app.db.repositories import AuditRepository, FeedbackRepository
from backend.app.schemas import ReviewDecisionRequest, UserIdentity

router = APIRouter(prefix="/decisions", tags=["decisions"])
Reviewer = Annotated[
    UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))
]


@router.get("/{incident_id}/review")
async def pending_review(
    incident_id: str, request: Request, actor: Reviewer
) -> dict[str, Any]:
    result = await request.app.state.review_service.pending_review(incident_id, actor)
    result["degradation"] = request.app.state.degradation_state.snapshot()
    return result


@router.post("/{incident_id}")
async def decide(
    incident_id: str,
    body: ReviewDecisionRequest,
    request: Request,
    actor: Reviewer,
) -> dict[str, Any]:
    return await request.app.state.review_service.decide(incident_id, body, actor)


@router.get("/{incident_id}/audit")
async def audit_chain(
    incident_id: str, request: Request, _: Reviewer
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        events = await AuditRepository(session).list_for_incident(incident_id)
        decisions = await FeedbackRepository(session).list_for_incident(incident_id)
    return {
        "incident_id": incident_id,
        "events": [
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "actor": row.actor,
                "payload": row.payload,
                "trace_id": row.trace_id,
                "timestamp": row.timestamp.isoformat(),
            }
            for row in events
        ],
        "decisions": [
            {
                "decision_id": row.decision_id,
                "decision": row.decision,
                "status": row.status,
                "actor_username": row.actor_username,
                "reason_code": row.reason_code,
                "final_action": row.final_action,
                "outcome": row.outcome,
                "decided_at": row.decided_at.isoformat(),
            }
            for row in decisions
        ],
    }
