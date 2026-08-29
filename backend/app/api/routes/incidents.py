"""Authenticated incident query endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from backend.app.core.runtime import AppError
from backend.app.core.security import require_roles
from backend.app.db.repositories import IncidentRepository
from backend.app.schemas import UserIdentity

router = APIRouter(prefix="/incidents", tags=["incidents"])
Analyst = Annotated[
    UserIdentity, Depends(require_roles("analyst", "lead_analyst", "admin"))
]


def _incident(row: Any) -> dict[str, Any]:
    return {
        "incident_id": row.incident_id,
        "alert_id": row.alert_id,
        "status": row.status,
        "severity": row.severity,
        "detected_at": row.detected_at.isoformat(),
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "reason": row.reason,
        "detector_output": row.detector_output,
        "exposure_estimate_inr": row.exposure_estimate_inr,
        "trace_id": row.trace_id,
        "segments": [
            {
                "rank": segment.rank,
                "conditions": segment.conditions,
                "support": segment.support,
                "baseline_support": segment.baseline_support,
                "risk_density": segment.risk_density,
                "baseline_risk_density": segment.baseline_risk_density,
                "density_lift": segment.density_lift,
                "prevalence_lift": segment.prevalence_lift,
                "excess_risk_contribution": segment.excess_risk_contribution,
                "p_value": segment.p_value,
                "score": segment.rank_score,
                "condition_contributions": segment.condition_contributions,
            }
            for segment in row.segments
        ],
    }


@router.get("")
async def list_incidents(
    request: Request,
    _: Analyst,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        repository = IncidentRepository(session)
        rows = await repository.list(status=status, limit=limit, offset=offset)
        count = await repository.count(status=status)
    return {"items": [_incident(row) for row in rows], "count": count}


@router.get("/{incident_id}")
async def incident_detail(incident_id: str, request: Request, _: Analyst) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        row, timeline = await IncidentRepository(session).get_with_timeline(incident_id)
    if row is None:
        raise AppError("incident_not_found", 404, "Incident does not exist")
    verification_payloads = [
        item.payload.get("payload", {})
        for item in timeline
        if item.kind == "agent_output"
        and item.payload.get("agent_name") == "evidence_verification"
    ]
    verdicts = _verification_verdicts(verification_payloads)
    rendered = []
    for item in timeline:
        payload = dict(item.payload)
        if item.kind == "agent_output":
            payload["payload"] = _display_payload(
                str(payload.get("agent_name", "")),
                payload.get("payload", {}),
                verdicts,
            )
        rendered.append(
            {"timestamp": item.timestamp.isoformat(), "kind": item.kind, "payload": payload}
        )
    result = _incident(row)
    result["timeline"] = rendered
    return result


def _verification_verdicts(
    payloads: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return one unambiguous verifier projection; missing or multiple runs fail closed."""

    if len(payloads) != 1:
        return {}
    result = payloads[0].get("result", {})
    rows = result.get("verdicts", []) if isinstance(result, dict) else []
    return {
        str(item["claim_id"]): item
        for item in rows
        if isinstance(item, dict) and item.get("claim_id")
    }


def _display_payload(
    agent_name: str,
    stored_payload: Any,
    verdicts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return citation-safe analyst output while retaining raw payloads in storage."""

    payload = dict(stored_payload) if isinstance(stored_payload, dict) else {}
    if agent_name == "evidence_verification":
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {**payload, "result": {}}
        safe_verdicts = [
            {
                "claim_id": item.get("claim_id"),
                "verdict": item.get("verdict", "unsupported"),
                "resolved_evidence_ids": list(item.get("resolved_evidence_ids", [])),
            }
            for item in result.get("verdicts", [])
            if isinstance(item, dict) and item.get("claim_id")
        ]
        return {**payload, "result": {**result, "verdicts": safe_verdicts}}
    if agent_name != "root_cause_hypotheses":
        return payload
    safe_hypotheses = []
    hypotheses = payload.get("result", [])
    for hypothesis in hypotheses if isinstance(hypotheses, list) else []:
        if not isinstance(hypothesis, dict):
            continue
        claim_id = str(hypothesis.get("claim_id", ""))
        verification = verdicts.get(claim_id, {})
        if verification.get("verdict") != "supported":
            continue
        safe_hypotheses.append(
            {
                **hypothesis,
                "evidence_ids": list(verification.get("resolved_evidence_ids", [])),
                "verification_verdict": "supported",
            }
        )
    return {**payload, "result": safe_hypotheses}


@router.get("/{incident_id}/investigation")
async def investigation_detail(
    incident_id: str, request: Request, _: Analyst
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        repository = IncidentRepository(session)
        incident = await repository.get(incident_id)
        if incident is None:
            raise AppError("incident_not_found", 404, "Incident does not exist")
        evidence = await repository.list_evidence(incident_id)
        outputs = await repository.list_agent_outputs(incident_id)
    verification_payloads = [
        row.payload for row in outputs if row.agent_name == "evidence_verification"
    ]
    verdicts = _verification_verdicts(verification_payloads)
    return {
        "incident_id": incident_id,
        "status": incident.status,
        "evidence": [
            {
                "evidence_id": row.evidence_id,
                "evidence_type": row.evidence_type,
                "source": row.source,
                "strength": row.strength,
                "payload": row.payload,
            }
            for row in evidence
        ],
        "outputs": [
            {
                "agent_name": row.agent_name,
                "status": row.status,
                "model_name": row.model_name,
                "prompt_version": row.prompt_version,
                "evidence_hash": row.evidence_hash,
                "payload": _display_payload(row.agent_name, row.payload, verdicts),
            }
            for row in outputs
        ],
    }
