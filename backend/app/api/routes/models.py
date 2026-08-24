"""Authenticated model registry and human-gated response-policy lifecycle."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from backend.app.core.security import require_roles
from backend.app.db.repositories import TransactionRepository
from backend.app.schemas import UserIdentity

router = APIRouter(prefix="/models", tags=["models"])
Viewer = Annotated[
    UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))
]
Admin = Annotated[UserIdentity, Depends(require_roles("admin"))]


@router.get("")
async def list_models(request: Request, _: Viewer) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        rows = await TransactionRepository(session).list_model_versions()
    return {
        "items": [
            {
                "model_version_id": row.model_version_id,
                "name": row.name,
                "version": row.version,
                "model_type": row.model_type,
                "artifact_uri": row.artifact_uri,
                "status": row.status,
                "threshold_score_space": row.threshold_score_space,
                "risk_density_score_space": row.risk_density_score_space,
                "metrics": row.metrics,
                "registered_at": row.registered_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/policies")
async def list_policies(request: Request, _: Viewer) -> dict[str, Any]:
    return await request.app.state.response_policy_service.list_versions()


@router.get("/policies/comparison")
async def compare_policies(request: Request, _: Viewer) -> dict[str, Any]:
    return await request.app.state.response_policy_service.comparison()


@router.post("/policies/{policy_version_id}/promote")
async def promote_policy(
    policy_version_id: int, request: Request, actor: Admin
) -> dict[str, Any]:
    return await request.app.state.response_policy_service.promote(
        policy_version_id, actor
    )


@router.post("/policies/{policy_version_id}/rollback")
async def rollback_policy(
    policy_version_id: int, request: Request, actor: Admin
) -> dict[str, Any]:
    return await request.app.state.response_policy_service.rollback(
        policy_version_id, actor
    )
