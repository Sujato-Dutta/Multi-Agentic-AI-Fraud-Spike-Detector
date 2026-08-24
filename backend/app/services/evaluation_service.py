"""Deterministic INR reward calculation and idempotent outcome learning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.config import Settings, get_settings
from backend.app.db.repositories import (
    FeedbackRepository,
    LearningRepository,
    OutboxRepository,
)
from backend.app.hitl.review_service import decision_dict
from backend.app.streaming.topics import TopicSet

ResponseAction = Literal[
    "no_action",
    "enhanced_monitoring",
    "step_up_verification",
    "temporary_defensive_rule",
    "manual_review",
    "human_escalation",
]
RESPONSE_ACTIONS: tuple[ResponseAction, ...] = (
    "no_action",
    "enhanced_monitoring",
    "step_up_verification",
    "temporary_defensive_rule",
    "manual_review",
    "human_escalation",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionEffect(StrictModel):
    fraud_stop_rate: float = Field(ge=0, le=1)
    legitimate_block_rate: float = Field(ge=0, le=1)
    friction_per_legitimate_customer: float = Field(ge=0)
    analyst_review_load: float = Field(ge=0)
    delay_hours: float = Field(ge=0)


class ActionEffects(StrictModel):
    version: str
    source: Literal["explicit_counterfactual_assumptions"]
    currency: Literal["INR"]
    notice: str = Field(min_length=20)
    actions: dict[ResponseAction, ActionEffect]

    @model_validator(mode="after")
    def exact_action_space(self) -> ActionEffects:
        if set(self.actions) != set(RESPONSE_ACTIONS):
            raise ValueError("Action effects must define exactly the six response actions")
        return self

    @classmethod
    def from_yaml(cls, path: Path | str) -> ActionEffects:
        with Path(path).open(encoding="utf-8") as stream:
            return cls.model_validate(yaml.safe_load(stream))


class RewardWeights(StrictModel):
    alpha: float = Field(1.0, ge=0)
    beta: float = Field(1.0, ge=0)
    gamma: float = Field(1.0, ge=0)
    delta: float = Field(1.0, ge=0)


class RewardResult(StrictModel):
    action: ResponseAction
    assumptions_version: str
    assumptions_source: Literal["explicit_counterfactual_assumptions"]
    assumptions_notice: str
    currency: Literal["INR"]
    fraud_prevented_inr: float
    false_positive_cost_inr: float
    friction_cost_inr: float
    review_cost_inr: float
    delay_cost_inr: float
    total_reward_inr: float
    affected_legitimate_customers: int
    assumptions: dict[str, Any]
    weights: RewardWeights


class RewardCalculator:
    """Pure financial calculator; all effects are explicit versioned assumptions."""

    def __init__(
        self, effects: ActionEffects, settings: Settings | None = None
    ) -> None:
        self.effects = effects
        self.settings = settings or get_settings()
        self.weights = RewardWeights(
            alpha=self.settings.reward_alpha,
            beta=self.settings.reward_beta,
            gamma=self.settings.reward_gamma,
            delta=self.settings.reward_delta,
        )

    def calculate(
        self,
        action: ResponseAction,
        transactions: Sequence[Mapping[str, Any]],
        *,
        weights: RewardWeights | None = None,
    ) -> RewardResult:
        if action not in self.effects.actions:
            raise ValueError(f"Unknown response action: {action}")
        effect = self.effects.actions[action]
        current = weights or self.weights
        fraud_loss = 0.0
        false_positive = 0.0
        legitimate_customers: set[str] = set()
        for row in transactions:
            is_fraud = row.get("is_fraud")
            if type(is_fraud) not in (int, bool) or int(is_fraud) not in (0, 1):
                raise ValueError("Reward transactions require a binary is_fraud label")
            if int(is_fraud) == 1:
                fraud_loss += _money(row, "fraud_loss_if_missed_inr")
            else:
                false_positive += _money(row, "false_positive_cost_if_blocked_inr")
                legitimate_customers.add(str(row.get("customer_id", row.get("transaction_id"))))
        fraud_prevented = fraud_loss * effect.fraud_stop_rate
        fp_cost = false_positive * effect.legitimate_block_rate
        friction = (
            len(legitimate_customers)
            * effect.friction_per_legitimate_customer
            * self.settings.customer_friction_cost_inr
        )
        review = effect.analyst_review_load * self.settings.analyst_review_cost_inr
        delay = effect.delay_hours * self.settings.detection_delay_cost_per_hour_inr
        total = (
            fraud_prevented
            - current.alpha * fp_cost
            - current.beta * friction
            - current.gamma * review
            - current.delta * delay
        )
        return RewardResult(
            action=action,
            assumptions_version=self.effects.version,
            assumptions_source=self.effects.source,
            assumptions_notice=self.effects.notice,
            currency=self.effects.currency,
            fraud_prevented_inr=round(fraud_prevented, 2),
            false_positive_cost_inr=round(fp_cost, 2),
            friction_cost_inr=round(friction, 2),
            review_cost_inr=round(review, 2),
            delay_cost_inr=round(delay, 2),
            total_reward_inr=round(total, 2),
            affected_legitimate_customers=len(legitimate_customers),
            assumptions=effect.model_dump(mode="json"),
            weights=current,
        )

    def counterfactuals(
        self, transactions: Sequence[Mapping[str, Any]]
    ) -> list[RewardResult]:
        return [self.calculate(action, transactions) for action in RESPONSE_ACTIONS]


class EvaluationService:
    """Consume authoritative outcomes into atomic reward, memory, and outbox rows."""

    def __init__(
        self,
        *,
        session_factory: Any,
        calculator: RewardCalculator,
        publisher: Any | None = None,
        topics: TopicSet | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.calculator = calculator
        self.topics = topics

    async def handle_outcome(self, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        decision_id = str(payload["decision_id"])
        async with self.session_factory() as session:
            decision = await FeedbackRepository(session).get(decision_id)
            if decision is None or decision.outcome is None or decision.status != "outcome_recorded":
                raise ValueError("Outcome event has no authoritative recorded decision")
            canonical = decision_dict(decision)
            compared = (
                "decision_id",
                "incident_id",
                "decision",
                "reason_code",
                "original_recommendation",
                "final_action",
                "outcome",
            )
            if any(payload.get(key) != canonical.get(key) for key in compared):
                raise ValueError("Outcome event disagrees with the authoritative decision")

            incident_id = decision.incident_id
            action = str(decision.final_action["action"])
            if action not in RESPONSE_ACTIONS:
                raise ValueError("Outcome references an unknown response action")
            outcome = dict(decision.outcome)
            effect = self.calculator.effects.actions[action]
            fraud_prevented = (
                float(outcome.get("fraud_loss_inr", 0.0))
                if outcome.get("outcome_code") == "prevented_loss"
                else 0.0
            )
            fp_cost = float(outcome.get("false_positive_cost_inr", 0.0))
            review = (
                effect.analyst_review_load
                * self.calculator.settings.analyst_review_cost_inr
            )
            delay = (
                effect.delay_hours
                * self.calculator.settings.detection_delay_cost_per_hour_inr
            )
            total = fraud_prevented - self.calculator.weights.alpha * fp_cost
            total -= (
                self.calculator.weights.gamma * review
                + self.calculator.weights.delta * delay
            )
            components = {
                "fraud_prevented_inr": round(fraud_prevented, 2),
                "false_positive_cost_inr": round(fp_cost, 2),
                "friction_cost_inr": 0.0,
                "review_cost_inr": round(review, 2),
                "delay_cost_inr": round(delay, 2),
                "weights": self.calculator.weights.model_dump(mode="json"),
                "assumptions": effect.model_dump(mode="json"),
                "assumptions_source": self.calculator.effects.source,
                "assumptions_notice": self.calculator.effects.notice,
                "currency": self.calculator.effects.currency,
                "source": "observed_analyst_outcome_with_declared_operational_assumptions",
            }
            repository = LearningRepository(session)
            idempotency_key = (
                f"outcome:{decision_id}:{self.calculator.effects.version}"
            )
            row, created = await repository.record_reward(
                incident_id=incident_id,
                decision_id=decision_id,
                action=action,
                total_reward=round(total, 2),
                components=components,
                idempotency_key=idempotency_key,
                assumptions_version=self.calculator.effects.version,
            )
            if created:
                recommendation = decision.original_recommendation.get(
                    "recommendation", {}
                )
                await repository.upsert_memory(
                    incident_id=incident_id,
                    summary=(
                        f"{decision.decision} / "
                        f"{outcome.get('outcome_code', 'unknown')}"
                    ),
                    attributes={
                        "conditions": recommendation.get("conditions", []),
                        "scenario_signature": recommendation.get("action", action),
                        "amount_band": recommendation.get("amount_band", "unknown"),
                        "analyst_decision": decision.decision,
                        "reason_code": decision.reason_code,
                        "final_action": action,
                        "decision_id": decision_id,
                        "reward_id": row.reward_id,
                        "assumptions_version": row.assumptions_version,
                        "total_reward_inr": row.total_reward,
                    },
                    outcome_tags=[outcome.get("outcome_code", "unknown"), action],
                )
            event_payload = {
                "reward_id": row.reward_id,
                "incident_id": incident_id,
                "decision_id": decision_id,
                "action": action,
                "total_reward_inr": row.total_reward,
                "components": row.components,
                "assumptions_version": row.assumptions_version,
            }
            if self.topics is not None:
                await OutboxRepository(session).enqueue_once(
                    event_id=f"reward:{idempotency_key}",
                    topic=self.topics.rewards,
                    event_type="reward.calculated",
                    payload=event_payload,
                    trace_id=incident_id,
                    message_key=incident_id,
                )
            await session.commit()
        return {**event_payload, "created": created}


def _money(row: Mapping[str, Any], field: str) -> float:
    value = row.get(field)
    if type(value) not in (int, float) or not 0 <= float(value) < float("inf"):
        raise ValueError(f"Reward transaction requires non-negative finite {field}")
    return float(value)
