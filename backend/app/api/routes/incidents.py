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
        if item.kind == "agent_output":
            agent_name = str(item.payload.get("agent_name", ""))
            safe_payload = _display_payload(
                agent_name,
                item.payload.get("payload", {}),
                verdicts,
            )
            rendered.append(
                {
                    "timestamp": item.timestamp.isoformat(),
                    "kind": "agent_output",
                    "payload": {
                        "agent_name": agent_name,
                        "status": item.payload.get("status", "completed"),
                        "result": safe_payload.get("result"),
                    },
                }
            )
        elif item.kind == "analyst_decision":
            rendered.append(
                {
                    "timestamp": item.timestamp.isoformat(),
                    "kind": "analyst_decision",
                    "payload": {
                        key: item.payload.get(key)
                        for key in (
                            "actor_username",
                            "decision",
                            "status",
                            "reason_code",
                            "reason_text",
                            "final_action",
                            "outcome",
                        )
                    },
                }
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
    """Return an allowlisted analyst projection; execution metadata stays in storage."""

    payload = dict(stored_payload) if isinstance(stored_payload, dict) else {}
    result = payload.get("result")
    if agent_name == "evidence_verification":
        if not isinstance(result, dict):
            return {"result": {}}
        safe_verdicts = [
            {
                "claim_id": item.get("claim_id"),
                "verdict": item.get("verdict", "unsupported"),
                "resolved_evidence_ids": list(item.get("resolved_evidence_ids", [])),
            }
            for item in result.get("verdicts", [])
            if isinstance(item, dict) and item.get("claim_id")
        ]
        return {
            "result": {
                "verdicts": safe_verdicts,
                "grounding_score": result.get("grounding_score"),
                "rejected_claim_count": result.get("rejected_claim_count"),
            }
        }
    if agent_name == "root_cause_hypotheses":
        safe_hypotheses = []
        for hypothesis in result if isinstance(result, list) else []:
            if not isinstance(hypothesis, dict):
                continue
            claim_id = str(hypothesis.get("claim_id", ""))
            verification = verdicts.get(claim_id, {})
            if verification.get("verdict") != "supported":
                continue
            safe_hypotheses.append(
                {
                    "claim_id": claim_id,
                    "statement": hypothesis.get("statement") or hypothesis.get("hypothesis"),
                    "strength": hypothesis.get("strength"),
                    "evidence_ids": list(verification.get("resolved_evidence_ids", [])),
                    "verification_verdict": "supported",
                }
            )
        return {"result": safe_hypotheses}
    if agent_name == "lead_spike_analysis" and isinstance(result, dict):
        return {"result": {"summary": result.get("summary")}}
    if agent_name == "segment_interpretation" and isinstance(result, dict):
        return {
            "result": {
                "name": result.get("name"),
                "description": result.get("description"),
                "evidence_ids": list(result.get("evidence_ids", [])),
            }
        }
    if agent_name == "deterministic_impact" and isinstance(result, dict):
        allowed = (
            "transaction_count",
            "fraud_exposure_inr",
            "false_positive_exposure_inr",
            "affected_legitimate_value_inr",
        )
        return {"result": {key: result.get(key) for key in allowed}}
    if agent_name == "response_recommendations":
        safe_responses = [
            {
                "rank": item.get("rank"),
                "action": item.get("action"),
                "rationale": item.get("rationale"),
                "evidence_ids": list(item.get("evidence_ids", [])),
            }
            for item in (result if isinstance(result, list) else [])
            if isinstance(item, dict)
        ]
        return {"result": safe_responses}
    if isinstance(result, dict):
        summary = result.get("summary") or result.get("analyst_summary")
        return {"result": {"summary": summary} if summary else {}}
    return {"result": [] if isinstance(result, list) else None}


def _evidence_summary(evidence_type: str, stored_payload: Any) -> dict[str, Any]:
    """Return only facts intentionally displayed by the analyst evidence cards."""

    payload = dict(stored_payload) if isinstance(stored_payload, dict) else {}
    fields = {
        "window_statistics": (
            "transaction_count",
            "density_lift",
            "volume_lift",
            "amount_sum_inr",
        ),
        "segment_statistics": ("support", "density_lift", "conditions"),
        "historical_baseline": ("baseline_density", "expected_high_risk_rate"),
        "similar_incidents": ("count",),
        "impact_estimate": (
            "transaction_count",
            "fraud_exposure_inr",
            "false_positive_exposure_inr",
        ),
    }.get(evidence_type, ())
    if evidence_type == "incident_memory":
        return {"count": len(payload.get("items", []))}
    return {key: payload.get(key) for key in fields if payload.get(key) is not None}


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
                "strength": row.strength,
                "summary": _evidence_summary(row.evidence_type, row.payload),
            }
            for row in evidence
        ],
        "outputs": [
            {
                "agent_name": row.agent_name,
                "status": row.status,
                "payload": _display_payload(row.agent_name, row.payload, verdicts),
            }
            for row in outputs
        ],
    }
