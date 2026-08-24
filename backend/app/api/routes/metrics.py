"""Dashboard metrics, drift, reports, and the authenticated WebSocket stream."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select

from backend.app.core.runtime import AppError
from backend.app.core.security import decode_access_token, require_roles
from backend.app.db.models import FraudScore, Incident, Transaction
from backend.app.monitoring.prometheus import ACTIVE_INCIDENTS, observe_dependencies
from backend.app.schemas import UserIdentity

router = APIRouter(tags=["metrics"])
Viewer = Annotated[
    UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))
]


@router.get("/metrics/summary")
async def dashboard_metrics(request: Request, _: Viewer) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        transactions = int(await session.scalar(select(func.count()).select_from(Transaction)) or 0)
        scores = int(await session.scalar(select(func.count()).select_from(FraudScore)) or 0)
        incidents = int(await session.scalar(select(func.count()).select_from(Incident)) or 0)
        active = int(
            await session.scalar(
                select(func.count()).select_from(Incident).where(Incident.status != "closed")
            )
            or 0
        )
        exposure = float(
            await session.scalar(select(func.coalesce(func.sum(Incident.exposure_estimate_inr), 0.0)))
            or 0.0
        )
        high_risk = int(
            await session.scalar(
                select(func.count())
                .select_from(FraudScore)
                .where(FraudScore.decision_score >= FraudScore.decision_threshold)
            )
            or 0
        )
    ACTIVE_INCIDENTS.set(active)
    dependencies = request.app.state.degradation_state.snapshot()
    observe_dependencies(dependencies)
    return {
        "transactions": transactions,
        "scores": scores,
        "incidents": incidents,
        "active_incidents": active,
        "high_risk_transactions": high_risk,
        "estimated_exposure_inr": exposure,
        "service": request.app.state.transaction_service.stats(),
        "cache": request.app.state.cache.stats(),
        "dependencies": dependencies,
    }


@router.get("/metrics/timeseries")
async def risk_timeseries(
    request: Request,
    _: Viewer,
    buckets: int = Query(60, ge=5, le=500),
) -> dict[str, Any]:
    """Return recent per-transaction risk points; the UI aggregates them for the trend."""

    async with request.app.state.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(
                        Transaction.timestamp,
                        Transaction.amount_inr,
                        Transaction.known_promo_event,
                        FraudScore.risk_probability,
                        FraudScore.decision_score,
                        FraudScore.decision_threshold,
                    )
                    .join(FraudScore, FraudScore.transaction_id == Transaction.transaction_id)
                    .order_by(Transaction.timestamp.desc())
                    .limit(buckets * 20)
                )
            ).all()
        )
        incidents = list(
            (
                await session.scalars(
                    select(Incident).order_by(Incident.detected_at.desc()).limit(25)
                )
            ).all()
        )
    points = [
        {
            "timestamp": row.timestamp.isoformat(),
            "amount_inr": float(row.amount_inr),
            "risk_probability": float(row.risk_probability),
            "high_risk": bool(row.decision_score >= row.decision_threshold),
            "known_promo_event": bool(row.known_promo_event),
        }
        for row in reversed(rows)
    ]
    return {
        "points": points,
        "windows": [
            {
                "incident_id": row.incident_id,
                "status": row.status,
                "severity": row.severity,
                "window_start": row.window_start.isoformat(),
                "window_end": row.window_end.isoformat(),
                "detected_at": row.detected_at.isoformat(),
                "density_lift": float((row.detector_output or {}).get("density_lift", 0.0)),
                "volume_lift": float((row.detector_output or {}).get("volume_lift", 0.0)),
            }
            for row in reversed(incidents)
        ],
    }


@router.get("/metrics/drift")
async def drift_snapshot(request: Request, _: Viewer) -> dict[str, Any]:
    monitor = getattr(request.app.state, "drift_monitor", None)
    if monitor is None:
        return {
            "available": False,
            "reason": "drift_reference_unavailable",
            "auto_retrain": False,
            "auto_policy_change": False,
            "features": [],
        }
    await monitor.refresh()
    return {"available": True, **monitor.snapshot()}


@router.get("/metrics/heldout")
async def heldout_report(request: Request, _: Viewer) -> dict[str, Any]:
    """Serve the sealed Phase 8 evaluation exactly as generated, or report its absence."""

    path = request.app.state.settings.heldout_report_path
    if not path.exists():
        return {
            "available": False,
            "reason": "heldout_evaluation_not_run",
            "path": str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppError(
            "heldout_report_unreadable", 503, f"Held-out report could not be read: {exc}"
        ) from exc
    return {"available": True, "path": str(path), "report": payload}


@router.websocket("/ws")
async def websocket_metrics(websocket: WebSocket, token: str = Query(...)) -> None:
    decode_access_token(token, websocket.app.state.settings)
    hub = websocket.app.state.websocket_hub
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
