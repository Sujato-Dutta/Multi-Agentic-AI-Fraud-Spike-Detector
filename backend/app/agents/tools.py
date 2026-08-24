"""Read-only deterministic evidence and financial-impact tools."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any

from backend.app.agents.state import EvidenceRecord, ImpactEstimate
from backend.app.cache.keys import hash_evidence
from backend.app.config import Settings
from backend.app.db.models import Incident, Transaction


def evidence_record(
    incident_id: str,
    evidence_type: str,
    source: str,
    payload: dict[str, Any],
    strength: str = "strong",
) -> EvidenceRecord:
    digest = hashlib.sha256(
        f"{incident_id}\0{evidence_type}\0{source}\0{hash_evidence(payload)}".encode()
    ).hexdigest()[:16]
    return EvidenceRecord(
        evidence_id=f"EVD-{digest}",
        evidence_type=evidence_type,
        source=source,
        strength=strength,
        payload=payload,
    )


def get_window_stats(incident: Incident, transactions: Sequence[Transaction]) -> EvidenceRecord:
    probabilities = [float(row.score.risk_probability) for row in transactions if row.score]
    high_risk = [
        row for row in transactions if row.score and row.score.decision_score >= row.score.decision_threshold
    ]
    payload = {
        "window_start": incident.window_start.isoformat(),
        "window_end": incident.window_end.isoformat(),
        "transaction_count": len(transactions),
        "risk_density": sum(probabilities) / max(len(probabilities), 1),
        "high_risk_count": len(high_risk),
        "amount_sum_inr": sum(float(row.amount_inr) for row in transactions),
        "amount_mean_inr": (
            sum(float(row.amount_inr) for row in transactions) / max(len(transactions), 1)
        ),
        "amount_max_inr": max((float(row.amount_inr) for row in transactions), default=0.0),
        "max_risk_probability": max(probabilities, default=0.0),
        "density_lift": float(incident.detector_output.get("density_lift", 0.0)),
        "volume_lift": float(incident.detector_output.get("volume_lift", 0.0)),
    }
    return evidence_record(incident.incident_id, "window_statistics", "detector_window", payload)


def get_segment_stats(incident: Incident) -> list[EvidenceRecord]:
    if not incident.segments:
        return [
            evidence_record(
                incident.incident_id,
                "segment_statistics",
                "deterministic_segmentation",
                {
                    "name": "elevated-risk transaction window",
                    "rank": 1,
                    "conditions": [],
                    "support": int(incident.detector_output.get("transaction_count", 0)),
                    "density_lift": float(incident.detector_output.get("density_lift", 0.0)),
                    "fallback_segment": True,
                },
                "moderate",
            )
        ]
    return [
        evidence_record(
            incident.incident_id,
            "segment_statistics",
            "deterministic_segmentation",
            {
                "name": " & ".join(segment.conditions),
                "rank": segment.rank,
                "conditions": list(segment.conditions),
                "support": segment.support,
                "baseline_support": segment.baseline_support,
                "risk_density": segment.risk_density,
                "baseline_risk_density": segment.baseline_risk_density,
                "density_lift": segment.density_lift,
                "prevalence_lift": segment.prevalence_lift,
                "excess_risk_contribution": segment.excess_risk_contribution,
                "p_value": segment.p_value,
            },
        )
        for segment in incident.segments
    ]


def get_historical_baseline(incident: Incident) -> EvidenceRecord:
    payload = {
        "baseline_density": float(incident.detector_output.get("baseline_density", 0.0)),
        "expected_high_risk_rate": float(
            incident.detector_output.get("expected_high_risk_rate", 0.0)
        ),
        "required_lift": float(incident.detector_output.get("required_lift", 0.0)),
        "promo_share": float(incident.detector_output.get("promo_share", 0.0)),
        "drift_psi": float(incident.detector_output.get("drift_psi", 0.0)),
    }
    return evidence_record(
        incident.incident_id, "historical_baseline", "risk_density_detector", payload
    )


def get_similar_incidents(
    incident: Incident, similar: Sequence[Incident]
) -> EvidenceRecord:
    payload = {
        "count": len(similar),
        "incidents": [
            {
                "incident_id": row.incident_id,
                "status": row.status,
                "reason": row.reason,
                "density_lift": float(row.detector_output.get("density_lift", 0.0)),
            }
            for row in similar
        ],
    }
    strength = "moderate" if similar else "weak"
    return evidence_record(
        incident.incident_id, "similar_incidents", "historical_incident_store", payload, strength
    )


def get_cost_estimate(
    incident: Incident,
    transactions: Sequence[Transaction],
    settings: Settings,
) -> tuple[ImpactEstimate, EvidenceRecord]:
    conditions = list(incident.segments[0].conditions) if incident.segments else []
    selected = [row for row in transactions if _matches_all(row, conditions)]
    if not selected:
        selected = list(transactions)
        conditions = []
    segment_name = " & ".join(conditions) if conditions else "elevated-risk transaction window"
    fraud_exposure = sum(
        float(row.score.risk_probability) * float(row.amount_inr) * settings.exposure_loss_factor
        for row in selected
        if row.score
    )
    false_positive_exposure = sum(
        (1.0 - float(row.score.risk_probability))
        * settings.investigation_false_positive_cost_inr
        for row in selected
        if row.score
    )
    affected_legitimate_value = sum(
        (1.0 - float(row.score.risk_probability)) * float(row.amount_inr)
        for row in selected
        if row.score
    )
    impact = ImpactEstimate(
        segment_name=segment_name,
        transaction_count=len(selected),
        fraud_exposure_inr=round(fraud_exposure, 2),
        false_positive_exposure_inr=round(false_positive_exposure, 2),
        affected_legitimate_value_inr=round(affected_legitimate_value, 2),
        calculation_method="deterministic_probability_weighted",
    )
    record = evidence_record(
        incident.incident_id,
        "impact_estimate",
        "deterministic_cost_engine",
        impact.model_dump(mode="json"),
    )
    return impact, record


def _matches_all(transaction: Transaction, conditions: Sequence[str]) -> bool:
    return all(_matches(transaction, condition) for condition in conditions)


def _matches(transaction: Transaction, condition: str) -> bool:
    if "=" not in condition:
        return False
    field, expected = condition.split("=", 1)
    direct = {
        "is_new_device",
        "is_proxy_ip",
        "billing_shipping_mismatch",
        "payment_method",
        "channel",
        "merchant_category",
    }
    if field in direct:
        actual = getattr(transaction, field)
        return str(actual).lower() == expected.lower()
    if field == "ip_cluster_group":
        return transaction.ip_cluster_id[:4] == expected
    if not field.endswith("_band"):
        return False
    numeric_field = field.removesuffix("_band")
    allowed = {
        "amount_inr",
        "txn_velocity_1h",
        "geo_distance_km",
        "customer_age_days",
        "account_changes_24h",
        "failed_attempts_24h",
    }
    if numeric_field not in allowed:
        return False
    return _in_interval(float(getattr(transaction, numeric_field)), expected)


def _in_interval(value: float, label: str) -> bool:
    if len(label) < 5 or label[0] not in "([" or label[-1] not in ")]":
        return False
    try:
        lower_text, upper_text = (part.strip() for part in label[1:-1].split(",", 1))
        lower = -math.inf if lower_text == "-inf" else float(lower_text)
        upper = math.inf if upper_text == "inf" else float(upper_text)
    except (ValueError, TypeError):
        return False
    lower_ok = value >= lower if label[0] == "[" else value > lower
    upper_ok = value <= upper if label[-1] == "]" else value < upper
    return lower_ok and upper_ok


def get_incident_memory(
    incident: Incident, memories: Sequence[dict[str, Any]]
) -> EvidenceRecord:
    """Expose prior human outcomes as advisory evidence, never authorization input."""

    return evidence_record(
        incident.incident_id,
        "incident_memory",
        "analyst_outcome_memory",
        {"items": list(memories), "advisory_only": True},
        "moderate" if memories else "weak",
    )
