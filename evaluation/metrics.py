"""Canonical transaction, cost, business, and spike-event metrics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)


@dataclass(frozen=True)
class TransactionMetrics:
    precision: float
    recall: float
    f1: float
    pr_auc: float
    roc_auc: float
    false_positive_rate: float
    false_negative_rate: float
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class CostMetrics:
    false_positive_cost_inr: float
    fraud_loss_missed_inr: float
    fraud_exposure_captured_inr: float
    legitimate_value_disrupted_inr: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class EventMetrics:
    precision: float
    recall: float
    matched_events: int
    total_events: int
    false_alerts: int
    continuation_alerts: int
    benign_window_false_alerts: int
    mean_delay_minutes: float | None
    median_delay_minutes: float | None
    p90_delay_minutes: float | None
    false_alerts_per_day: float | None
    matches: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["matches"] = list(self.matches)
        return value


def safe_divide(numerator: float, denominator: float, *, empty_value: float = 0.0) -> float:
    return float(numerator / denominator) if denominator else float(empty_value)


def transaction_metrics(
    y_true: Iterable[int], probabilities: Iterable[float], threshold: float
) -> TransactionMetrics:
    truth = np.asarray(list(y_true), dtype=int)
    scores = np.asarray(list(probabilities), dtype=float)
    if len(truth) != len(scores) or not len(truth):
        raise ValueError("Truth and probability vectors must have equal non-zero length")
    if not 0 <= threshold <= 1 or np.any((scores < 0) | (scores > 1)):
        raise ValueError("Threshold and probabilities must be within [0, 1]")
    predicted = (scores >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(truth, predicted, labels=[0, 1]).ravel()
    roc_auc = float(roc_auc_score(truth, scores)) if len(np.unique(truth)) == 2 else float("nan")
    return TransactionMetrics(
        precision=float(precision_score(truth, predicted, zero_division=0)),
        recall=float(recall_score(truth, predicted, zero_division=0)),
        f1=float(f1_score(truth, predicted, zero_division=0)),
        pr_auc=float(average_precision_score(truth, scores)),
        roc_auc=roc_auc,
        false_positive_rate=safe_divide(fp, fp + tn),
        false_negative_rate=safe_divide(fn, fn + tp),
        true_positives=int(tp),
        false_positives=int(fp),
        true_negatives=int(tn),
        false_negatives=int(fn),
    )


def cost_metrics(
    labels: pd.DataFrame,
    predictions: Iterable[int],
    *,
    amounts: Iterable[float] | None = None,
) -> CostMetrics:
    predicted = np.asarray(list(predictions), dtype=int)
    truth = labels["is_fraud"].to_numpy(dtype=int)
    if len(predicted) != len(labels):
        raise ValueError("Prediction and label lengths differ")
    fp = (predicted == 1) & (truth == 0)
    fn = (predicted == 0) & (truth == 1)
    tp = (predicted == 1) & (truth == 1)
    amount_values = np.zeros(len(labels)) if amounts is None else np.asarray(list(amounts), dtype=float)
    if len(amount_values) != len(labels):
        raise ValueError("Amount and label lengths differ")
    return CostMetrics(
        false_positive_cost_inr=float(labels.loc[fp, "false_positive_cost_if_blocked_inr"].sum()),
        fraud_loss_missed_inr=float(labels.loc[fn, "fraud_loss_if_missed_inr"].sum()),
        fraud_exposure_captured_inr=float(labels.loc[tp, "fraud_loss_if_missed_inr"].sum()),
        legitimate_value_disrupted_inr=float(amount_values[fp].sum()),
    )


def _normalise_alerts(alerts: pd.DataFrame | Iterable[Mapping[str, object]]) -> pd.DataFrame:
    frame = alerts.copy() if isinstance(alerts, pd.DataFrame) else pd.DataFrame(list(alerts))
    if frame.empty:
        return pd.DataFrame(columns=["alert_id", "fire_timestamp"])
    if "fire_timestamp" not in frame:
        raise ValueError("Alerts require fire_timestamp")
    frame["fire_timestamp"] = pd.to_datetime(frame["fire_timestamp"])
    if "alert_id" not in frame:
        frame["alert_id"] = [f"A{i + 1}" for i in range(len(frame))]
    return frame.sort_values("fire_timestamp", kind="stable").reset_index(drop=True)


def event_metrics(
    alerts: pd.DataFrame | Iterable[Mapping[str, object]],
    events: pd.DataFrame,
    *,
    benign_events: pd.DataFrame | None = None,
    grace_minutes: int = 30,
    stream_start: pd.Timestamp | None = None,
    stream_end: pd.Timestamp | None = None,
) -> EventMetrics:
    alert_frame = _normalise_alerts(alerts)
    event_frame = events.copy()
    for column in ("start_timestamp", "end_timestamp"):
        event_frame[column] = pd.to_datetime(event_frame[column])
    event_frame = event_frame.sort_values("start_timestamp", kind="stable").reset_index(drop=True)
    benign = benign_events.copy() if benign_events is not None else pd.DataFrame()
    if not benign.empty:
        benign["start_timestamp"] = pd.to_datetime(benign["start_timestamp"])
        benign["end_timestamp"] = pd.to_datetime(benign["end_timestamp"])

    matched_event_ids: set[str] = set()
    matches: list[dict[str, object]] = []
    false_times: list[pd.Timestamp] = []
    continuations = 0
    grace = timedelta(minutes=grace_minutes)

    for alert in alert_frame.itertuples(index=False):
        timestamp = pd.Timestamp(alert.fire_timestamp)
        candidates = event_frame.loc[
            event_frame["start_timestamp"].le(timestamp)
            & event_frame["end_timestamp"].add(grace).ge(timestamp)
        ]
        unmatched = candidates.loc[~candidates["event_id"].isin(matched_event_ids)]
        if not unmatched.empty:
            event = unmatched.iloc[0]
            event_id = str(event["event_id"])
            matched_event_ids.add(event_id)
            delay = (timestamp - event["start_timestamp"]).total_seconds() / 60
            matches.append(
                {
                    "event_id": event_id,
                    "alert_id": str(alert.alert_id),
                    "fire_timestamp": timestamp.isoformat(),
                    "delay_minutes": float(delay),
                }
            )
        elif not candidates.empty:
            continuations += 1
        else:
            false_times.append(timestamp)

    benign_false_alerts = 0
    for timestamp in false_times:
        if not benign.empty and (
            benign["start_timestamp"].le(timestamp) & benign["end_timestamp"].ge(timestamp)
        ).any():
            benign_false_alerts += 1

    delays = np.asarray([match["delay_minutes"] for match in matches], dtype=float)
    matched = len(matches)
    false_count = len(false_times)
    duration_days = None
    if stream_start is not None and stream_end is not None:
        duration_days = max((pd.Timestamp(stream_end) - pd.Timestamp(stream_start)).total_seconds() / 86400, 0)
    return EventMetrics(
        precision=safe_divide(matched, matched + false_count, empty_value=1.0),
        recall=safe_divide(matched, len(event_frame), empty_value=1.0),
        matched_events=matched,
        total_events=len(event_frame),
        false_alerts=false_count,
        continuation_alerts=continuations,
        benign_window_false_alerts=benign_false_alerts,
        mean_delay_minutes=float(np.mean(delays)) if len(delays) else None,
        median_delay_minutes=float(np.median(delays)) if len(delays) else None,
        p90_delay_minutes=float(np.percentile(delays, 90)) if len(delays) else None,
        false_alerts_per_day=(safe_divide(false_count, duration_days) if duration_days else None),
        matches=tuple(matches),
    )


def net_risk_benefit(
    fraud_loss_prevented_inr: float,
    false_positive_cost_inr: float,
    review_cost_inr: float,
    friction_cost_inr: float,
) -> float:
    return float(
        fraud_loss_prevented_inr
        - false_positive_cost_inr
        - review_cost_inr
        - friction_cost_inr
    )


def metric_contract_check() -> None:
    """Small executable sanity check used by `scripts/run_evaluation.py --check`."""

    events = pd.DataFrame(
        [{"event_id": "E1", "start_timestamp": "2026-01-01 10:00", "end_timestamp": "2026-01-01 11:00"}]
    )
    result = event_metrics([{"fire_timestamp": "2026-01-01 10:15"}], events)
    if result.matched_events != 1 or result.median_delay_minutes != 15:
        raise AssertionError("Event-matching metric contract failed")
    txn = transaction_metrics([0, 1], [0.1, 0.9], 0.5)
    if txn.f1 != 1.0:
        raise AssertionError("Transaction metric contract failed")
