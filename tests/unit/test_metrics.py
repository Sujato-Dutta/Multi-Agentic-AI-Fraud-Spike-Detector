from __future__ import annotations

import pandas as pd
import pytest

from evaluation.metrics import (
    cost_metrics,
    event_metrics,
    net_risk_benefit,
    transaction_metrics,
)


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "event_id": "E1",
                "start_timestamp": "2026-01-01 10:00",
                "end_timestamp": "2026-01-01 11:00",
            },
            {
                "event_id": "E2",
                "start_timestamp": "2026-01-01 14:00",
                "end_timestamp": "2026-01-01 15:00",
            },
        ]
    )


def test_event_matching_boundaries_continuations_and_benign_false_alerts() -> None:
    alerts = [
        {"alert_id": "A1", "fire_timestamp": "2026-01-01 10:00"},  # exact start
        {"alert_id": "A2", "fire_timestamp": "2026-01-01 10:30"},  # continuation
        {"alert_id": "A3", "fire_timestamp": "2026-01-01 15:30"},  # exact grace end
        {"alert_id": "A4", "fire_timestamp": "2026-01-01 16:00"},  # false, benign
    ]
    benign = pd.DataFrame(
        [{"event_id": "B1", "start_timestamp": "2026-01-01 15:45", "end_timestamp": "2026-01-01 16:30"}]
    )
    result = event_metrics(
        alerts,
        _events(),
        benign_events=benign,
        stream_start=pd.Timestamp("2026-01-01 00:00"),
        stream_end=pd.Timestamp("2026-01-03 00:00"),
    )

    assert result.matched_events == 2
    assert result.recall == 1.0
    assert result.precision == pytest.approx(2 / 3)
    assert result.continuation_alerts == 1
    assert result.false_alerts == 1
    assert result.benign_window_false_alerts == 1
    assert result.median_delay_minutes == 45.0
    assert result.p90_delay_minutes == pytest.approx(81.0)
    assert result.false_alerts_per_day == 0.5


def test_event_after_grace_is_late_and_zero_alert_conventions_are_explicit() -> None:
    one_event = _events().iloc[:1]
    late = event_metrics([{"fire_timestamp": "2026-01-01 11:31"}], one_event)
    empty = event_metrics([], one_event)
    no_events = event_metrics([], one_event.iloc[:0])

    assert late.matched_events == 0 and late.false_alerts == 1
    assert empty.precision == 1.0 and empty.recall == 0.0
    assert no_events.precision == 1.0 and no_events.recall == 1.0
    assert empty.median_delay_minutes is None


def test_transaction_and_cost_metrics_use_expected_denominators_and_rows() -> None:
    truth = [0, 0, 1, 1]
    probabilities = [0.8, 0.1, 0.9, 0.2]
    metrics = transaction_metrics(truth, probabilities, 0.5)
    labels = pd.DataFrame(
        {
            "is_fraud": truth,
            "false_positive_cost_if_blocked_inr": [100.0, 200.0, 0.0, 0.0],
            "fraud_loss_if_missed_inr": [0.0, 0.0, 500.0, 700.0],
        }
    )
    costs = cost_metrics(labels, [1, 0, 1, 0], amounts=[1000, 2000, 3000, 4000])

    assert metrics.precision == metrics.recall == metrics.f1 == 0.5
    assert metrics.false_positive_rate == metrics.false_negative_rate == 0.5
    assert costs.false_positive_cost_inr == 100.0
    assert costs.fraud_loss_missed_inr == 700.0
    assert costs.fraud_exposure_captured_inr == 500.0
    assert costs.legitimate_value_disrupted_inr == 1000.0
    assert net_risk_benefit(1000, 100, 50, 25) == 825.0
