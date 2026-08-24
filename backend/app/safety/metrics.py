"""Shared degradation telemetry for the Phase 5 safety matrix."""

from prometheus_client import Counter

DEGRADATION_EVENTS = Counter(
    "fraud_degradation_events_total",
    "Visible safety degradation events by acceptance-matrix case.",
    ("case",),
)


def record_degradation(case: str, amount: int = 1) -> None:
    if amount > 0:
        DEGRADATION_EVENTS.labels(case=case).inc(amount)
