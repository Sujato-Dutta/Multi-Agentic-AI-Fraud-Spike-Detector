"""Single catalog for domain metrics; infrastructure metrics are re-exported here.

Metrics owned by a specific subsystem (cache, streaming, LLM, safety, review) stay defined beside
that subsystem to avoid import cycles, but every family is indexed by ``metric_catalog`` so there is
one place to enumerate the observable surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from backend.app.cache.cache_service import (
    CACHE_FALLBACKS_TOTAL,
    CACHE_HITS_TOTAL,
    CACHE_MISSES_TOTAL,
    CACHE_REDIS_AVAILABLE,
    CACHE_REDIS_FAILURES_TOTAL,
)
from backend.app.llm.gateway import (
    LLM_CALLS,
    LLM_COST,
    LLM_FALLBACKS,
    LLM_LATENCY,
    LLM_TOKENS,
)
from backend.app.safety.evidence_grounding import GROUNDING_CLAIMS
from backend.app.safety.metrics import DEGRADATION_EVENTS
from backend.app.streaming.consumer import (
    STREAM_CONSUMED,
    STREAM_CONSUMER_LAG,
    STREAM_HANDLER_FAILURES,
)
from backend.app.streaming.producer import STREAM_PUBLISH_FAILURES, STREAM_PUBLISHED

_STATUS_VALUE = {"healthy": 1.0, "degraded": 0.5, "down": 0.0}

TRANSACTIONS_INGESTED = Counter(
    "fraud_transactions_ingested_total",
    "Transactions accepted by the scoring path.",
    ("outcome",),
)
TRANSACTION_RISK = Histogram(
    "fraud_transaction_risk_probability",
    "Calibrated risk probability per scored transaction.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0),
)
HIGH_RISK_TRANSACTIONS = Counter(
    "fraud_high_risk_transactions_total",
    "Transactions at or above the operating decision threshold.",
)
SPIKE_ALERTS = Counter(
    "fraud_spike_alerts_total",
    "Risk-density spike alerts promoted to incidents.",
    ("severity",),
)
DETECTION_DELAY_MINUTES = Histogram(
    "fraud_detection_delay_minutes",
    "Alert fire time minus window start, in dataset minutes.",
    buckets=(15, 30, 45, 60, 90, 120, 180, 240, 480),
)
ACTIVE_INCIDENTS = Gauge(
    "fraud_active_incidents", "Incidents that are not closed."
)
ESTIMATED_EXPOSURE_INR = Gauge(
    "fraud_estimated_exposure_inr",
    "Deterministic exposure estimate for the most recent incident.",
)
INVESTIGATIONS = Counter(
    "fraud_investigations_total",
    "Completed agent investigations by terminal status.",
    ("status",),
)
INVESTIGATION_LATENCY = Histogram(
    "fraud_investigation_latency_seconds",
    "Wall-clock latency of one investigation graph run.",
    buckets=(1, 2.5, 5, 10, 15, 20, 30, 60, 120),
)
DECISION_LATENCY = Histogram(
    "fraud_decision_latency_seconds",
    "Latency from analyst decision submission to durable completion.",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 60),
)
DRIFT_PSI = Gauge(
    "fraud_drift_psi",
    "Population stability index versus the training reference distribution.",
    ("feature",),
)
DRIFT_ALERT_ACTIVE = Gauge(
    "fraud_drift_alert_active",
    "Advisory drift alert flag; never mutates a model or policy.",
    ("feature",),
)
DEPENDENCY_STATUS = Gauge(
    "fraud_dependency_status",
    "Dependency health as 1 healthy, 0.5 degraded, 0 down.",
    ("dependency",),
)


def observe_transaction(
    *, created: bool, risk_probability: float, decision_score: float, threshold: float
) -> None:
    TRANSACTIONS_INGESTED.labels(outcome="created" if created else "duplicate").inc()
    if not created:
        return
    TRANSACTION_RISK.observe(max(0.0, min(1.0, float(risk_probability))))
    if float(decision_score) >= float(threshold):
        HIGH_RISK_TRANSACTIONS.inc()


def observe_incident(payload: Mapping[str, Any]) -> None:
    """Record one newly created incident; delay uses dataset minutes, not wall clock."""

    SPIKE_ALERTS.labels(severity=str(payload.get("severity", "unknown"))).inc()
    detector = payload.get("detector_output") or {}
    delay = detector.get("detection_delay_minutes")
    if isinstance(delay, (int, float)) and delay >= 0:
        DETECTION_DELAY_MINUTES.observe(float(delay))
    exposure = payload.get("exposure_estimate_inr")
    if isinstance(exposure, (int, float)):
        ESTIMATED_EXPOSURE_INR.set(float(exposure))


def observe_dependencies(snapshot: Mapping[str, Mapping[str, Any]]) -> None:
    for dependency, health in snapshot.items():
        status = str(health.get("status", "down"))
        DEPENDENCY_STATUS.labels(dependency=dependency).set(_STATUS_VALUE.get(status, 0.0))


def metric_catalog() -> dict[str, tuple[str, ...]]:
    """Enumerate every exported metric family, grouped by operational category."""

    return {
        "traffic": (
            TRANSACTIONS_INGESTED._name,
            TRANSACTION_RISK._name,
            HIGH_RISK_TRANSACTIONS._name,
        ),
        "detection": (
            SPIKE_ALERTS._name,
            DETECTION_DELAY_MINUTES._name,
            ACTIVE_INCIDENTS._name,
            ESTIMATED_EXPOSURE_INR._name,
        ),
        "agents": (
            INVESTIGATIONS._name,
            INVESTIGATION_LATENCY._name,
            LLM_CALLS._name,
            LLM_TOKENS._name,
            LLM_COST._name,
            LLM_LATENCY._name,
            LLM_FALLBACKS._name,
            GROUNDING_CLAIMS._name,
        ),
        "hitl": (DECISION_LATENCY._name,),
        "ml": (DRIFT_PSI._name, DRIFT_ALERT_ACTIVE._name),
        "cache": (
            CACHE_HITS_TOTAL._name,
            CACHE_MISSES_TOTAL._name,
            CACHE_REDIS_FAILURES_TOTAL._name,
            CACHE_FALLBACKS_TOTAL._name,
            CACHE_REDIS_AVAILABLE._name,
        ),
        "streaming": (
            STREAM_PUBLISHED._name,
            STREAM_PUBLISH_FAILURES._name,
            STREAM_CONSUMED._name,
            STREAM_HANDLER_FAILURES._name,
            STREAM_CONSUMER_LAG._name,
        ),
        "reliability": (DEPENDENCY_STATUS._name, DEGRADATION_EVENTS._name),
    }
