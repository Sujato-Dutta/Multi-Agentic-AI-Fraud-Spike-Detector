from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from backend.app.config import Settings
from backend.app.ml.spike_detection.detector import RiskDensitySpikeDetector
from backend.app.ml.spike_detection.windows import (
    SlidingWindowAggregator,
    WindowSnapshot,
)


def _window(
    end: str | pd.Timestamp,
    *,
    count: int = 40,
    density: float = 0.02,
    high_risk_count: int = 1,
    volume_per_hour: float = 20.0,
    promo_share: float = 0.0,
) -> WindowSnapshot:
    timestamp = pd.Timestamp(end)
    rows = pd.DataFrame(
        {
            "timestamp": [timestamp] * count,
            "risk_probability": [density] * count,
            "known_promo_event": [1 if promo_share else 0] * count,
            "amount_inr": [100.0] * count,
        }
    )
    return WindowSnapshot(
        start_timestamp=timestamp - timedelta(hours=2),
        end_timestamp=timestamp,
        transaction_count=count,
        risk_sum=count * density,
        risk_density=density,
        high_risk_count=high_risk_count,
        amount_sum_inr=count * 100.0,
        promo_share=promo_share,
        volume_per_hour=volume_per_hour,
        rows=rows,
    )


def _detector() -> RiskDensitySpikeDetector:
    settings = Settings(
        detector_lift_threshold=2.5,
        detector_extreme_lift=10.0,
        detector_alpha=0.05,
        detector_confirm_windows=2,
        detector_min_support=20,
        detector_promo_share_threshold=0.30,
        detector_promo_lift_margin=0.50,
        detector_cooldown_minutes=0,
    )
    detector = RiskDensitySpikeDetector(settings)
    reference = [
        _window(pd.Timestamp("2026-01-01 00:00") + timedelta(minutes=15 * index))
        for index in range(48)
    ]
    detector.prime(reference)
    return detector


def test_volume_only_benign_surge_never_fires_but_persistent_density_spike_does() -> None:
    benign = _detector()
    benign_alerts = [
        benign.process(
            _window(
                pd.Timestamp("2026-01-02 00:00") + timedelta(minutes=15 * index),
                count=100,
                density=0.02,
                high_risk_count=2,
                volume_per_hour=50.0,
            )
        )
        for index in range(4)
    ]
    assert all(alert is None for alert in benign_alerts)
    assert max(decision.volume_lift for decision in benign.decisions) >= 2.4

    fraud = _detector()
    first = fraud.process(
        _window("2026-01-02 00:00", count=100, density=0.16, high_risk_count=20, volume_per_hour=50)
    )
    second = fraud.process(
        _window("2026-01-02 00:15", count=100, density=0.16, high_risk_count=20, volume_per_hour=50)
    )
    assert first is None
    assert second is not None
    assert second.fire_timestamp == pd.Timestamp("2026-01-02 00:15")
    assert second.density_lift > 7


def test_support_gate_and_promo_context_is_not_a_veto() -> None:
    detector = _detector()
    assert detector.process(
        _window("2026-01-02 00:00", count=10, density=0.8, high_risk_count=10)
    ) is None

    first = detector.process(
        _window("2026-01-02 00:15", density=0.16, high_risk_count=12, promo_share=1.0)
    )
    second = detector.process(
        _window("2026-01-02 00:30", density=0.16, high_risk_count=12, promo_share=1.0)
    )
    assert first is None
    assert second is not None
    assert second.required_lift == 3.0
    assert "did not veto" in second.reason


def test_active_spike_does_not_poison_adaptive_baseline() -> None:
    detector = _detector()
    baseline_values = []
    for index in range(8):
        detector.process(
            _window(
                pd.Timestamp("2026-01-02 00:00") + timedelta(minutes=15 * index),
                density=0.16,
                high_risk_count=12,
            )
        )
        baseline_values.append(detector.decisions[-1].baseline_density)

    assert detector.active
    assert max(baseline_values) == pytest.approx(min(baseline_values), rel=0.05)
    assert max(baseline_values) < 0.03
    assert np.mean([decision.density_lift for decision in detector.decisions]) > 5


def test_slide_boundary_transactions_follow_open_start_closed_end_contract() -> None:
    aggregator = SlidingWindowAggregator(window_minutes=30, slide_minutes=15)
    emitted = []
    rows = [
        ("T0", "2026-01-01 00:00"),
        ("T1", "2026-01-01 00:15"),
        ("T2", "2026-01-01 00:30"),
        ("T3", "2026-01-01 00:30"),
    ]
    for transaction_id, timestamp in rows:
        emitted.extend(
            aggregator.add(
                {
                    "transaction_id": transaction_id,
                    "timestamp": pd.Timestamp(timestamp),
                    "amount_inr": 100.0,
                    "known_promo_event": 0,
                },
                risk_probability=0.02,
                decision_score=0.01,
                decision_threshold=0.5,
            )
        )
    emitted.extend(aggregator.flush())

    first = next(window for window in emitted if window.end_timestamp == pd.Timestamp("2026-01-01 00:30"))
    assert first.transaction_count == 3
    assert first.rows["transaction_id"].tolist() == ["T1", "T2", "T3"]


def test_unsupported_windows_do_not_poison_baseline_or_mask_next_spike() -> None:
    detector = _detector()
    for index in range(8):
        assert detector.process(
            _window(
                pd.Timestamp("2026-01-02 00:00") + timedelta(minutes=15 * index),
                count=10,
                density=0.80,
                high_risk_count=10,
            )
        ) is None
    assert max(decision.baseline_density for decision in detector.decisions) < 0.03

    detector.process(_window("2026-01-02 02:00", density=0.16, high_risk_count=12))
    alert = detector.process(_window("2026-01-02 02:15", density=0.16, high_risk_count=12))
    assert alert is not None
