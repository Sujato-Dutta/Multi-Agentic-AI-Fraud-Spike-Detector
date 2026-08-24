"""Stable feature, label, and cost contracts."""

LABEL_COLUMNS = frozenset(
    {
        "is_fraud",
        "is_spike_injected",
        "injected_spike_event_id",
        "benign_event_id",
        "is_within_spike_window",
        "active_spike_event_id",
        "fraud_scenario_family",
        "false_positive_cost_if_blocked_inr",
        "fraud_loss_if_missed_inr",
    }
)
RAW_ID_COLUMNS = frozenset(
    {"transaction_id", "customer_id", "merchant_id", "device_id", "ip_cluster_id"}
)
NON_MODEL_COLUMNS = RAW_ID_COLUMNS | {"timestamp"}
CATEGORICAL_COLUMNS = ("merchant_category", "payment_method", "channel", "ip_cluster_group")

COST_ASSUMPTION_DESCRIPTIONS = {
    "analyst_review_cost_inr": "Synthetic operational cost per incident sent to human review.",
    "customer_friction_cost_inr": "Synthetic friction proxy per legitimate customer stepped up.",
    "detection_delay_cost_per_hour_inr": "Synthetic loss-of-response-speed proxy per hour.",
}
