from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from prometheus_client import Counter

from backend.app.safety.metrics import record_degradation
from backend.app.safety.policy_engine import PolicyContext

GROUNDING_CLAIMS = Counter(
    "fraud_grounding_claims_total",
    "Material claims accepted or stripped by deterministic grounding.",
    ("status",),
)
AUTHORIZATION_BASIS_VERSION = "deterministic-authorization-v1"
_SAFE_WORKFLOWS = {"no_action", "human_escalation"}
_EXECUTING_ACTIONS = {
    "enhanced_monitoring",
    "manual_review",
    "step_up_verification",
    "temporary_defensive_rule",
}


def ground_claims(
    claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip unsupported, contradicted, duplicate, and unresolved material claims."""

    available = {str(item["evidence_id"]) for item in evidence}
    verdicts = {
        str(item["claim_id"]): item
        for item in verification.get("verdicts", [])
        if isinstance(item, Mapping) and item.get("claim_id")
    }
    claim_ids = [str(item.get("claim_id", "")) for item in claims]
    duplicates = {item for item in claim_ids if not item or claim_ids.count(item) > 1}
    supported: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for claim in claims:
        claim_id = str(claim.get("claim_id", ""))
        cited = list(dict.fromkeys(str(item) for item in claim.get("evidence_ids", [])))
        verdict = verdicts.get(claim_id, {})
        resolved = [item for item in cited if item in available]
        accepted = (
            claim_id not in duplicates
            and bool(cited)
            and len(resolved) == len(cited)
            and verdict.get("verdict") == "supported"
        )
        if accepted:
            supported.append({**claim, "evidence_ids": resolved})
            GROUNDING_CLAIMS.labels(status="supported").inc()
            continue
        reason = str(verdict.get("verdict", "unresolved"))
        rejected.append({"claim_id": claim_id, "reason": reason})
        GROUNDING_CLAIMS.labels(status="rejected").inc()
    total = len(claims)
    record_degradation("unsupported_claim", len(rejected))
    return {
        "claims": supported,
        "total_claim_count": total,
        "supported_claim_count": len(supported),
        "rejected_claim_count": len(rejected),
        "rejected_claims": rejected,
        "grounding_score": len(supported) / max(total, 1),
    }


def build_authorization_context(
    action: str,
    evidence: Sequence[Mapping[str, Any]],
    impact: Mapping[str, Any],
    *,
    actor_role: str | None = None,
) -> tuple[PolicyContext, dict[str, Any]]:
    """Build policy inputs exclusively from deterministic, internally consistent records."""

    if action in _SAFE_WORKFLOWS:
        context = PolicyContext(
            affected_legitimate_value_inr=0,
            fraud_exposure_inr=0,
            segment_breadth=0,
            grounding_score=1,
            confidence_score=1,
            novelty_score=1,
            actor_role=actor_role,
        )
        return context, {
            "version": AUTHORIZATION_BASIS_VERSION,
            "valid": True,
            "evidence_ids": [],
            "reasons": ["non_executing_workflow"],
        }

    reasons: list[str] = []
    if action == "promote_policy":
        reasons.append("candidate_policy_evaluation_missing")
    elif action not in _EXECUTING_ACTIONS:
        reasons.append("unsupported_action")

    def unique_record(
        evidence_type: str, source: str, *, rank_one: bool = False
    ) -> Mapping[str, Any] | None:
        rows = [
            item
            for item in evidence
            if item.get("evidence_type") == evidence_type
            and item.get("source") == source
            and (
                not rank_one
                or isinstance(item.get("payload"), Mapping)
                and type(item["payload"].get("rank")) is int
                and item["payload"]["rank"] == 1
            )
        ]
        if len(rows) != 1:
            reasons.append(f"invalid_{evidence_type}")
            return None
        return rows[0]

    window = unique_record("window_statistics", "detector_window")
    segment = unique_record(
        "segment_statistics", "deterministic_segmentation", rank_one=True
    )
    impact_record = unique_record("impact_estimate", "deterministic_cost_engine")
    similar = unique_record("similar_incidents", "historical_incident_store")
    evidence_ids = [
        str(item["evidence_id"])
        for item in (window, segment, impact_record, similar)
        if item is not None and item.get("evidence_id")
    ]

    def strict_count(value: Any, reason: str, *, positive: bool = False) -> int:
        if type(value) is not int or value < (1 if positive else 0):
            reasons.append(reason)
            return 0
        return value

    def strict_amount(value: Any, reason: str) -> float:
        if type(value) not in (int, float):
            reasons.append(reason)
            return 0.0
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0:
            reasons.append(reason)
            return 0.0
        return parsed

    window_payload = (window or {}).get("payload", {})
    segment_payload = (segment or {}).get("payload", {})
    similar_payload = (similar or {}).get("payload", {})
    stored_impact = (impact_record or {}).get("payload", {})
    for payload, reason in (
        (window_payload, "invalid_window_statistics"),
        (segment_payload, "invalid_segment_statistics"),
        (similar_payload, "invalid_similar_incidents"),
        (stored_impact, "invalid_impact_estimate"),
    ):
        if not isinstance(payload, Mapping):
            reasons.append(reason)

    window_count = strict_count(
        window_payload.get("transaction_count") if isinstance(window_payload, Mapping) else None,
        "invalid_window_count",
        positive=True,
    )
    segment_support = strict_count(
        segment_payload.get("support") if isinstance(segment_payload, Mapping) else None,
        "invalid_segment_support",
    )
    similar_count = strict_count(
        similar_payload.get("count") if isinstance(similar_payload, Mapping) else None,
        "invalid_similar_incident_count",
    )
    state_transaction_count = strict_count(
        impact.get("transaction_count"), "invalid_impact_transaction_count"
    )
    stored_transaction_count = strict_count(
        stored_impact.get("transaction_count")
        if isinstance(stored_impact, Mapping)
        else None,
        "invalid_stored_impact_transaction_count",
    )
    fraud_exposure = strict_amount(
        impact.get("fraud_exposure_inr"), "invalid_fraud_exposure"
    )
    stored_fraud_exposure = strict_amount(
        stored_impact.get("fraud_exposure_inr")
        if isinstance(stored_impact, Mapping)
        else None,
        "invalid_stored_fraud_exposure",
    )
    legitimate_value = strict_amount(
        impact.get("affected_legitimate_value_inr"), "invalid_legitimate_value"
    )
    stored_legitimate_value = strict_amount(
        stored_impact.get("affected_legitimate_value_inr")
        if isinstance(stored_impact, Mapping)
        else None,
        "invalid_stored_legitimate_value",
    )
    false_positive = strict_amount(
        impact.get("false_positive_exposure_inr"), "invalid_false_positive_exposure"
    )
    stored_false_positive = strict_amount(
        stored_impact.get("false_positive_exposure_inr")
        if isinstance(stored_impact, Mapping)
        else None,
        "invalid_stored_false_positive_exposure",
    )

    if segment_support > window_count:
        reasons.append("invalid_segment_support")
    segment_name = impact.get("segment_name")
    stored_segment_name = (
        stored_impact.get("segment_name") if isinstance(stored_impact, Mapping) else None
    )
    expected_segment_name = (
        segment_payload.get("name") if isinstance(segment_payload, Mapping) else None
    )
    method = impact.get("calculation_method")
    stored_method = (
        stored_impact.get("calculation_method")
        if isinstance(stored_impact, Mapping)
        else None
    )
    if (
        type(segment_name) is not str
        or not segment_name
        or segment_name != stored_segment_name
        or segment_name != expected_segment_name
        or method != "deterministic_probability_weighted"
        or stored_method != method
        or state_transaction_count != segment_support
        or stored_transaction_count != state_transaction_count
        or stored_fraud_exposure != fraud_exposure
        or stored_legitimate_value != legitimate_value
        or stored_false_positive != false_positive
    ):
        reasons.append("inconsistent_impact_estimate")

    valid = not reasons
    context = PolicyContext(
        affected_legitimate_value_inr=legitimate_value,
        fraud_exposure_inr=fraud_exposure,
        segment_breadth=(segment_support / window_count if window_count > 0 else 1),
        grounding_score=1 if valid else 0,
        confidence_score=1 if valid else 0,
        novelty_score=(1 if similar_count == 0 else 1 / (similar_count + 1)),
        actor_role=actor_role,
    )
    return context, {
        "version": AUTHORIZATION_BASIS_VERSION,
        "valid": valid,
        "evidence_ids": evidence_ids,
        "reasons": list(dict.fromkeys(reasons)),
    }
