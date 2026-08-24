"""JSON-safe typed investigation state and structured agent outputs."""

from __future__ import annotations

from typing import Any, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRecord(StrictModel):
    evidence_id: str
    evidence_type: str
    source: str
    strength: Literal["weak", "moderate", "strong"]
    payload: dict[str, Any]


class SpikeAnalysis(StrictModel):
    summary: str
    anomalies: list[str] = Field(min_length=1, max_length=5)
    evidence_ids: list[str] = Field(min_length=1)


class SegmentInterpretation(StrictModel):
    name: str
    description: str
    conditions: list[str]
    evidence_ids: list[str] = Field(min_length=1)


class RootCauseHypothesis(StrictModel):
    claim_id: str
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    strength: Literal["weak", "moderate", "strong"]


class HypothesisSet(StrictModel):
    hypotheses: list[RootCauseHypothesis] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def require_unique_claim_ids(self) -> Self:
        claim_ids = [item.claim_id for item in self.hypotheses]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Hypothesis claim IDs must be unique")
        return self


class VerificationSuggestion(StrictModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "contradicted"]
    rationale: str


class VerificationSuggestions(StrictModel):
    verdicts: list[VerificationSuggestion] = Field(min_length=1)


class VerificationVerdict(StrictModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "contradicted"]
    resolved_evidence_ids: list[str]
    rationale: str


class VerificationResult(StrictModel):
    verdicts: list[VerificationVerdict]
    grounding_score: float = Field(ge=0, le=1)
    rejected_claim_count: int = Field(ge=0)


class ImpactEstimate(StrictModel):
    segment_name: str
    transaction_count: int = Field(ge=0)
    fraud_exposure_inr: float = Field(ge=0)
    false_positive_exposure_inr: float = Field(ge=0)
    affected_legitimate_value_inr: float = Field(default=0.0, ge=0)
    calculation_method: Literal["deterministic_probability_weighted"]


class ResponseRecommendation(StrictModel):
    rank: int = Field(ge=1)
    action: Literal[
        "no_action",
        "enhanced_monitoring",
        "step_up_verification",
        "temporary_defensive_rule",
        "manual_review",
        "human_escalation",
    ]
    rationale: str
    requires_human_review: bool = True
    evidence_ids: list[str] = Field(min_length=1)


class ResponsePlan(StrictModel):
    responses: list[ResponseRecommendation] = Field(min_length=2, max_length=6)


class LeadSynthesis(StrictModel):
    summary: str
    confidence: Literal["low", "medium", "high"]
    escalation_posture: Literal["monitor", "review", "escalate"]
    evidence_ids: list[str] = Field(min_length=1)
    degraded: bool


class AlertExplanation(StrictModel):
    title: str
    analyst_summary: str
    next_step: str


class InvestigationState(TypedDict, total=False):
    incident_id: str
    trace_id: str
    detector_output: dict[str, Any]
    persisted_segments: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    spike_analysis: dict[str, Any]
    segment: dict[str, Any]
    hypotheses: list[dict[str, Any]]
    verification: dict[str, Any]
    impact: dict[str, Any]
    responses: list[dict[str, Any]]
    response_policy: dict[str, Any]
    grounded_claims: dict[str, Any]
    policy_context: dict[str, Any]
    policy_basis: dict[str, Any]
    policy_gate: dict[str, Any]
    human_review: dict[str, Any]
    synthesis: dict[str, Any]
    alert_explanation: dict[str, Any]
    provenance: list[dict[str, Any]]
    degraded: bool
    status: str
