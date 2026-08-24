"""Business-cost projection from transaction decisions."""

from __future__ import annotations

from typing import Any

from backend.app.config import Settings, get_settings
from evaluation.metrics import CostMetrics, net_risk_benefit


def evaluate_business_cost(
    costs: CostMetrics,
    *,
    reviewed_incidents: int = 0,
    stepped_up_legitimate_customers: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    review_cost = reviewed_incidents * settings.analyst_review_cost_inr
    friction_cost = stepped_up_legitimate_customers * settings.customer_friction_cost_inr
    benefit = net_risk_benefit(
        costs.fraud_exposure_captured_inr,
        costs.false_positive_cost_inr,
        review_cost,
        friction_cost,
    )
    return {
        **costs.to_dict(),
        "analyst_review_cost_inr": float(review_cost),
        "customer_friction_cost_inr": float(friction_cost),
        "net_risk_benefit_inr": benefit,
        "assumption_scope": "synthetic_evaluation_proxy",
    }
