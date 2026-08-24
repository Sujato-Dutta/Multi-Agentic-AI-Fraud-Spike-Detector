"""Sliding event-time windows for offline replay and streaming ingestion."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd


@dataclass(frozen=True)
class WindowSnapshot:
    start_timestamp: pd.Timestamp
    end_timestamp: pd.Timestamp
    transaction_count: int
    risk_sum: float
    risk_density: float
    high_risk_count: int
    amount_sum_inr: float
    promo_share: float
    volume_per_hour: float
    rows: pd.DataFrame

    def to_dict(self, include_rows: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "start_timestamp": self.start_timestamp.isoformat(),
            "end_timestamp": self.end_timestamp.isoformat(),
            "transaction_count": self.transaction_count,
            "risk_sum": self.risk_sum,
            "risk_density": self.risk_density,
            "high_risk_count": self.high_risk_count,
            "amount_sum_inr": self.amount_sum_inr,
            "promo_share": self.promo_share,
            "volume_per_hour": self.volume_per_hour,
        }
        if include_rows:
            result["rows"] = self.rows.to_dict(orient="records")
        return result


class SlidingWindowAggregator:
    """Build fixed-width, fixed-slide snapshots from chronologically arriving transactions."""

    def __init__(self, window_minutes: int = 120, slide_minutes: int = 15) -> None:
        if window_minutes <= 0 or slide_minutes <= 0 or slide_minutes > window_minutes:
            raise ValueError("Window/slide must be positive and slide cannot exceed window")
        self.window = timedelta(minutes=window_minutes)
        self.slide = timedelta(minutes=slide_minutes)
        self._records: deque[dict[str, object]] = deque()
        self._stream_start: pd.Timestamp | None = None
        self._next_emit: pd.Timestamp | None = None
        self._last_timestamp: pd.Timestamp | None = None

    def _snapshot(self, end: pd.Timestamp) -> WindowSnapshot:
        start = end - self.window
        selected = [
            row for row in self._records if start < pd.Timestamp(row["timestamp"]) <= end
        ]
        frame = pd.DataFrame(selected)
        count = len(frame)
        hours = self.window.total_seconds() / 3600
        if not count:
            frame = pd.DataFrame(
                columns=["timestamp", "risk_probability", "decision_score", "high_risk"]
            )
        risk_sum = float(frame["risk_probability"].sum()) if count else 0.0
        return WindowSnapshot(
            start_timestamp=start,
            end_timestamp=end,
            transaction_count=count,
            risk_sum=risk_sum,
            risk_density=risk_sum / count if count else 0.0,
            high_risk_count=int(frame["high_risk"].sum()) if count else 0,
            amount_sum_inr=float(frame["amount_inr"].sum()) if count else 0.0,
            promo_share=float(frame["known_promo_event"].mean()) if count else 0.0,
            volume_per_hour=count / hours,
            rows=frame.reset_index(drop=True),
        )

    def add(
        self,
        transaction: Mapping[str, object],
        *,
        risk_probability: float,
        decision_score: float,
        decision_threshold: float,
    ) -> list[WindowSnapshot]:
        timestamp = pd.Timestamp(transaction["timestamp"])
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("Transactions must arrive in non-decreasing event-time order")
        if not 0 <= risk_probability <= 1 or not 0 <= decision_score <= 1:
            raise ValueError("Risk probabilities and decision scores must be within [0, 1]")
        if self._stream_start is None:
            self._stream_start = timestamp
            self._next_emit = timestamp.ceil(pd.Timedelta(self.slide))

        emitted: list[WindowSnapshot] = []
        assert self._next_emit is not None and self._stream_start is not None
        while self._next_emit < timestamp:
            if self._next_emit - self._stream_start >= self.window:
                emitted.append(self._snapshot(self._next_emit))
            self._next_emit += self.slide

        cutoff = timestamp - self.window
        while self._records and pd.Timestamp(self._records[0]["timestamp"]) <= cutoff:
            self._records.popleft()
        record = dict(transaction)
        record.update(
            {
                "timestamp": timestamp,
                "risk_probability": float(risk_probability),
                "decision_score": float(decision_score),
                "high_risk": bool(decision_score >= decision_threshold),
            }
        )
        self._records.append(record)
        self._last_timestamp = timestamp
        return emitted

    def flush(self) -> list[WindowSnapshot]:
        """Emit the last complete slide ending at or before the final transaction."""

        if self._last_timestamp is None or self._next_emit is None or self._stream_start is None:
            return []
        emitted: list[WindowSnapshot] = []
        while self._next_emit <= self._last_timestamp:
            if self._next_emit - self._stream_start >= self.window:
                emitted.append(self._snapshot(self._next_emit))
            self._next_emit += self.slide
        return emitted


def build_sliding_windows(
    transactions: pd.DataFrame,
    risk_probabilities: list[float] | object,
    decision_scores: list[float] | object,
    decision_threshold: float,
    *,
    window_minutes: int = 120,
    slide_minutes: int = 15,
) -> list[WindowSnapshot]:
    """Replay a frame through the exact streaming aggregator used online."""

    probabilities = list(risk_probabilities)
    scores = list(decision_scores)
    if len(transactions) != len(probabilities) or len(transactions) != len(scores):
        raise ValueError("Transaction and score lengths differ")
    aggregator = SlidingWindowAggregator(window_minutes, slide_minutes)
    snapshots: list[WindowSnapshot] = []
    for (_, row), probability, score in zip(
        transactions.iterrows(), probabilities, scores, strict=True
    ):
        snapshots.extend(
            aggregator.add(
                row.to_dict(),
                risk_probability=float(probability),
                decision_score=float(score),
                decision_threshold=decision_threshold,
            )
        )
    snapshots.extend(aggregator.flush())
    return snapshots
