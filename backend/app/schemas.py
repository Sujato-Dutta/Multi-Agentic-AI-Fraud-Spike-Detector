"""Typed API and stream boundaries for Phase 3."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.safety.permissions import Action


class TransactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    timestamp: datetime
    customer_id: str
    merchant_id: str
    device_id: str
    ip_cluster_id: str
    amount_inr: float = Field(gt=0)
    merchant_category: str
    payment_method: str
    channel: str
    customer_age_days: int = Field(ge=0)
    customer_txn_count_30d: int = Field(ge=0)
    customer_avg_amount_30d: float = Field(ge=0)
    time_since_last_txn_min: float = Field(ge=0)
    txn_velocity_10m: int = Field(ge=0)
    txn_velocity_1h: int = Field(ge=0)
    amount_zscore_customer: float
    is_new_device: bool
    device_trust_score: float = Field(ge=0, le=1)
    ip_risk_score: float = Field(ge=0, le=1)
    geo_distance_km: float = Field(ge=0)
    billing_shipping_mismatch: bool
    failed_attempts_24h: int = Field(ge=0)
    account_changes_24h: int = Field(ge=0)
    is_proxy_ip: bool
    prior_disputes_90d: int = Field(ge=0)
    merchant_fraud_rate_7d: float = Field(ge=0, le=1)
    known_promo_event: bool
    hour: int = Field(ge=0, le=23)
    day_of_week: int = Field(ge=0, le=6)
    is_weekend: bool


class TransactionBatchInput(BaseModel):
    transactions: list[TransactionInput] = Field(min_length=1)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in_seconds: int
    role: str


class UserIdentity(BaseModel):
    username: str
    role: Literal["analyst", "lead_analyst", "admin"]


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "modify", "reject", "escalate"]
    reason_code: Literal[
        "confirmed_risk",
        "false_positive",
        "insufficient_evidence",
        "customer_impact",
        "segment_too_broad",
        "policy_violation",
        "needs_specialist",
        "other",
    ]
    reason_text: str | None = Field(default=None, max_length=1000)
    modified_action: Action | None = None

    @model_validator(mode="after")
    def validate_choice(self) -> ReviewDecisionRequest:
        if self.decision == "modify" and self.modified_action is None:
            raise ValueError("Modify requires modified_action")
        if self.decision != "modify" and self.modified_action is not None:
            raise ValueError("modified_action is valid only for Modify")
        if self.reason_code == "other" and not (self.reason_text or "").strip():
            raise ValueError("The 'other' reason requires reason_text")
        return self


class OutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_code: Literal[
        "fraud_confirmed",
        "legitimate",
        "mixed",
        "prevented_loss",
        "no_loss",
        "unknown",
    ]
    fraud_loss_inr: float = Field(default=0.0, ge=0)
    false_positive_cost_inr: float = Field(default=0.0, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "down"]
    dependencies: dict[str, dict[str, Any]]
    service: dict[str, Any]


class WebSocketMessage(BaseModel):
    type: Literal[
        "txn",
        "metric_tick",
        "alert",
        "incident_update",
        "decision_update",
        "audit_event",
        "degradation",
    ]
    timestamp: datetime
    payload: dict[str, Any]
