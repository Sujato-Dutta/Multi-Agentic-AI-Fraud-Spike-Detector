"""Production/candidate shadow scoring and deterministic promotion gates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.ml.policy.contextual_bandit import ACTIONS, LinUCBPolicy


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyMetrics(StrictModel):
    expected_reward_inr: float
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    false_positive_cost_inr: float = Field(ge=0)
    fraud_value_captured_inr: float = Field(ge=0)
    escalation_rate: float = Field(ge=0, le=1)
    safety_violations: int = Field(ge=0)
    evaluated_incidents: int = Field(ge=0)


class GateResult(StrictModel):
    passed: bool
    checks: dict[str, bool]
    reasons: list[str]


class PromotionGate:
    def __init__(
        self,
        *,
        reward_margin_inr: float,
        recall_tolerance: float,
        fp_cost_tolerance: float,
    ) -> None:
        self.reward_margin_inr = reward_margin_inr
        self.recall_tolerance = recall_tolerance
        self.fp_cost_tolerance = fp_cost_tolerance

    def evaluate(
        self, candidate: PolicyMetrics, production: PolicyMetrics
    ) -> GateResult:
        checks = {
            "reward_margin": candidate.expected_reward_inr
            >= production.expected_reward_inr + self.reward_margin_inr,
            "recall_tolerance": candidate.recall
            >= production.recall - self.recall_tolerance,
            "fp_cost_tolerance": candidate.false_positive_cost_inr
            <= production.false_positive_cost_inr * (1 + self.fp_cost_tolerance),
            "zero_safety_violations": candidate.safety_violations == 0,
            "measured_holdback": candidate.evaluated_incidents > 0,
        }
        reasons = [name for name, passed in checks.items() if not passed]
        return GateResult(passed=not reasons, checks=checks, reasons=reasons)


class ShadowPolicy:
    """Candidate output is observational; operative_action is always production."""

    def __init__(
        self,
        production: LinUCBPolicy | None,
        candidate: LinUCBPolicy | None = None,
        *,
        production_error: str | None = None,
        candidate_error: str | None = None,
    ) -> None:
        self.production = production
        self.candidate = candidate
        self.production_error = production_error
        self.candidate_error = candidate_error

    @classmethod
    def from_paths(
        cls,
        production_path: Path | str,
        candidate_path: Path | str,
        *,
        assumptions_version: str,
    ) -> ShadowPolicy:
        production, production_error = _safe_load_policy(
            Path(production_path), assumptions_version
        )
        candidate, candidate_error = _safe_load_policy(
            Path(candidate_path), assumptions_version
        )
        return cls(
            production,
            candidate,
            production_error=production_error,
            candidate_error=candidate_error,
        )

    def score(self, context: Mapping[str, Any]) -> dict[str, Any]:
        production_error = self.production_error
        try:
            production_ranking = (
                self.production.rank(context)
                if self.production is not None
                else _conservative_ranking()
            )
        except Exception as exc:  # noqa: BLE001 - serving must fail closed
            production_error = f"{type(exc).__name__}: {exc}"[:500]
            production_ranking = _conservative_ranking()
        candidate_error = self.candidate_error
        candidate_ranking = None
        if self.candidate is not None:
            try:
                candidate_ranking = self.candidate.rank(context)
            except Exception as exc:  # noqa: BLE001 - shadow failure is isolated
                candidate_error = f"{type(exc).__name__}: {exc}"[:500]
        return {
            "operative_action": production_ranking[0]["action"],
            "production_ranking": production_ranking,
            "candidate_ranking": candidate_ranking,
            "candidate_shadow_only": True,
            "candidate_degraded": candidate_error is not None,
            "candidate_error": candidate_error,
            "production_error": production_error,
            "degraded": self.production is None or production_error is not None,
        }


def policy_context_from_state(state: Mapping[str, Any]) -> dict[str, float]:
    evidence: Sequence[Mapping[str, Any]] = state.get("evidence", [])

    def payload(evidence_type: str) -> Mapping[str, Any]:
        return next(
            (
                item.get("payload", {})
                for item in evidence
                if item.get("evidence_type") == evidence_type
            ),
            {},
        )

    window = payload("window_statistics")
    segment = payload("segment_statistics")
    baseline = payload("historical_baseline")
    memories = payload("incident_memory").get("items", [])
    memory_rewards = [
        float(item.get("attributes", {}).get("total_reward_inr", 0.0))
        for item in memories
    ]
    memory_rejections = [
        item.get("attributes", {}).get("analyst_decision") == "reject"
        for item in memories
    ]
    count = max(int(window.get("transaction_count", 0)), 1)
    support = int(segment.get("support", 0))
    verification = state.get("verification", {})
    grounding = float(verification.get("grounding_score", 0.0))
    return {
        "fraud_probability_mean": float(window.get("risk_density", 0.0)),
        "fraud_probability_max": float(window.get("max_risk_probability", 0.0)),
        "density_lift": float(window.get("density_lift", 0.0)),
        "volume_lift": float(window.get("volume_lift", 0.0)),
        "segment_support": float(support),
        "segment_breadth": min(1.0, support / count),
        "amount_mean_inr": float(window.get("amount_mean_inr", 0.0)),
        "amount_max_inr": float(window.get("amount_max_inr", 0.0)),
        "agent_confidence": grounding,
        "grounding_score": grounding,
        "historical_segment_fraud_rate": float(
            baseline.get("expected_high_risk_rate", 0.0)
        ),
        "promotion_context": float(state.get("detector_output", {}).get("promo_share", 0.0)),
        "similar_incident_mean_reward": (
            sum(memory_rewards) / len(memory_rewards) if memory_rewards else 0.0
        ),
        "similar_incident_rejection_rate": (
            sum(memory_rejections) / len(memory_rejections) if memory_rejections else 0.0
        ),
    }


def _safe_load_policy(
    path: Path, assumptions_version: str
) -> tuple[LinUCBPolicy | None, str | None]:
    if not path.exists():
        return None, "artifact_missing"
    try:
        return LinUCBPolicy.load(path, assumptions_version=assumptions_version), None
    except Exception as exc:  # noqa: BLE001 - missing/corrupt models degrade safely
        return None, f"{type(exc).__name__}: {exc}"[:500]


def _conservative_ranking() -> list[dict[str, float | str]]:
    order = ("human_escalation", "manual_review", "enhanced_monitoring") + tuple(
        action
        for action in ACTIONS
        if action not in {"human_escalation", "manual_review", "enhanced_monitoring"}
    )
    return [
        {"action": action, "expected_reward_inr": 0.0}
        for action in order
    ]
