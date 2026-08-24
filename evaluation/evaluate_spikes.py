"""Event-level scoring for replayed spike alerts."""

from __future__ import annotations

from typing import Any

import pandas as pd

from evaluation.metrics import event_metrics
from evaluation.replay import ReplayResult


def evaluate_spike_replay(
    replay: ReplayResult,
    spike_events: pd.DataFrame,
    benign_events: pd.DataFrame,
    *,
    stream_start: pd.Timestamp,
    stream_end: pd.Timestamp,
    grace_minutes: int = 30,
) -> dict[str, Any]:
    alerts = [
        {"alert_id": alert.alert_id, "fire_timestamp": alert.fire_timestamp}
        for alert in replay.alerts
    ]
    metrics = event_metrics(
        alerts,
        spike_events,
        benign_events=benign_events,
        grace_minutes=grace_minutes,
        stream_start=stream_start,
        stream_end=stream_end,
    )
    return {
        "metrics": metrics.to_dict(),
        "alerts": [alert.to_dict() for alert in replay.alerts],
        "segments": replay.alert_segments,
        "reference_window_count": replay.reference_window_count,
        "evaluation_window_count": replay.evaluation_window_count,
        "scoring": {
            "score_space": replay.score_space,
            "degraded": replay.degraded,
            "reason": replay.degradation_reason,
        },
    }
