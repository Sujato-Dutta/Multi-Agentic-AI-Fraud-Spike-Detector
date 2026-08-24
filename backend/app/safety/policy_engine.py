"""Pure, YAML-configured, deny-by-default safety policy engine."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from backend.app.safety.escalation import Escalation, determine_escalation
from backend.app.safety.permissions import is_action_allowed


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GlobalRules(StrictModel):
    legitimate_value_ceiling_inr: float = Field(gt=0)
    segment_breadth_ceiling: float = Field(gt=0, le=1)
    grounding_score_floor: float = Field(ge=0, le=1)
    confidence_score_floor: float = Field(ge=0, le=1)


class EscalationRules(StrictModel):
    high_impact_inr: float = Field(gt=0)
    low_grounding_score: float = Field(ge=0, le=1)
    broad_segment_ratio: float = Field(gt=0, le=1)
    high_novelty_score: float = Field(ge=0, le=1)


class ActionRule(StrictModel):
    outcome: Literal["allow", "require_approval"]
    legitimate_value_ceiling_inr: float | None = Field(default=None, gt=0)
    segment_breadth_ceiling: float | None = Field(default=None, gt=0, le=1)


class PolicyRules(StrictModel):
    version: str
    global_: GlobalRules = Field(alias="global")
    escalation: EscalationRules
    actions: dict[str, ActionRule]


class PolicyContext(StrictModel):
    affected_legitimate_value_inr: float = Field(ge=0)
    fraud_exposure_inr: float = Field(ge=0)
    segment_breadth: float = Field(ge=0, le=1)
    grounding_score: float = Field(ge=0, le=1)
    confidence_score: float = Field(ge=0, le=1)
    novelty_score: float = Field(ge=0, le=1)
    actor_role: str | None = None


class PolicyDecision(StrictModel):
    decision: Literal["allow", "require_approval", "deny"]
    rule_id: str
    reason: str
    policy_version: str
    escalation: Escalation


class PolicyEngine:
    """Construction may load configuration; evaluation is pure and performs no I/O."""

    def __init__(self, rules: PolicyRules) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path | str) -> PolicyEngine:
        with Path(path).open(encoding="utf-8") as stream:
            payload = yaml.safe_load(stream)
        return cls(PolicyRules.model_validate(payload))

    @classmethod
    def default(cls) -> PolicyEngine:
        path = Path(__file__).parents[3] / "infrastructure" / "policies.yaml"
        return cls.from_yaml(path)

    def evaluate(
        self, action: str, context: PolicyContext | Mapping[str, Any]
    ) -> PolicyDecision:
        current = (
            context
            if isinstance(context, PolicyContext)
            else PolicyContext.model_validate(context)
        )
        escalation = self._escalation(current)
        rule = self.rules.actions.get(action)
        if rule is None:
            return self._deny("unknown_action", "Action is not allow-listed.", escalation)
        if current.actor_role and not is_action_allowed(current.actor_role, action):
            return self._deny(
                "role_action_mismatch",
                f"Role {current.actor_role!r} cannot authorize {action!r}.",
                "escalate",
            )
        if action in {"no_action", "human_escalation"}:
            return PolicyDecision(
                decision=rule.outcome,
                rule_id=f"safe_workflow:{action}",
                reason="Non-executing workflow action is permitted for human handling.",
                policy_version=self.rules.version,
                escalation=("escalate" if action == "human_escalation" else escalation),
            )
        if current.grounding_score < self.rules.global_.grounding_score_floor:
            return self._deny(
                "grounding_score_floor",
                "Grounding score is below the configured safety floor.",
                "escalate",
            )
        legitimate_ceiling = min(
            self.rules.global_.legitimate_value_ceiling_inr,
            rule.legitimate_value_ceiling_inr
            or self.rules.global_.legitimate_value_ceiling_inr,
        )
        if current.affected_legitimate_value_inr > legitimate_ceiling:
            return self._deny(
                "legitimate_value_ceiling",
                "Affected legitimate value exceeds the action ceiling.",
                "escalate",
            )
        breadth_ceiling = min(
            self.rules.global_.segment_breadth_ceiling,
            rule.segment_breadth_ceiling or self.rules.global_.segment_breadth_ceiling,
        )
        if current.segment_breadth > breadth_ceiling:
            return self._deny(
                "segment_breadth_ceiling",
                "Affected segment is broader than the action ceiling.",
                "escalate",
            )
        if current.confidence_score < self.rules.global_.confidence_score_floor:
            return PolicyDecision(
                decision="require_approval",
                rule_id="confidence_score_floor",
                reason="Confidence is below the automatic handling floor.",
                policy_version=self.rules.version,
                escalation=max_escalation(escalation, "require_approval"),
            )
        return PolicyDecision(
            decision=rule.outcome,
            rule_id=f"allowlist:{action}",
            reason="Action satisfies deterministic policy limits.",
            policy_version=self.rules.version,
            escalation=(
                "require_approval"
                if rule.outcome == "require_approval"
                else escalation
            ),
        )

    def _escalation(self, context: PolicyContext) -> Escalation:
        rules = self.rules.escalation
        return determine_escalation(
            impact_inr=context.fraud_exposure_inr,
            confidence_score=context.confidence_score,
            grounding_score=context.grounding_score,
            segment_breadth=context.segment_breadth,
            novelty_score=context.novelty_score,
            high_impact_inr=rules.high_impact_inr,
            low_grounding_score=rules.low_grounding_score,
            broad_segment_ratio=rules.broad_segment_ratio,
            high_novelty_score=rules.high_novelty_score,
        )

    def _deny(self, rule_id: str, reason: str, escalation: Escalation) -> PolicyDecision:
        return PolicyDecision(
            decision="deny",
            rule_id=rule_id,
            reason=reason,
            policy_version=self.rules.version,
            escalation=escalation,
        )


def max_escalation(left: Escalation, right: Escalation) -> Escalation:
    order: tuple[Escalation, ...] = ("auto_handle", "require_approval", "escalate")
    return order[max(order.index(left), order.index(right))]
