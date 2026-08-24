"""Offline chronological replay through the exact streaming detector path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from backend.app.config import Settings, get_settings
from backend.app.ml.fraud.predictor import FraudPredictor, ResilientFraudScorer
from backend.app.ml.spike_detection.detector import RiskDensitySpikeDetector, SpikeAlert
from backend.app.ml.spike_detection.segmentation import discover_segments
from backend.app.ml.spike_detection.windows import WindowSnapshot, build_sliding_windows

ReplayScorer = FraudPredictor | ResilientFraudScorer


@dataclass(frozen=True)
class ReplayResult:
    alerts: tuple[SpikeAlert, ...]
    alert_segments: dict[str, list[dict[str, object]]]
    decisions: tuple[dict[str, object], ...]
    reference_window_count: int
    evaluation_window_count: int
    score_space: str
    degraded: bool
    degradation_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alerts": [alert.to_dict() for alert in self.alerts],
            "alert_segments": self.alert_segments,
            "decisions": list(self.decisions),
            "reference_window_count": self.reference_window_count,
            "evaluation_window_count": self.evaluation_window_count,
            "scoring": {
                "score_space": self.score_space,
                "degraded": self.degraded,
                "reason": self.degradation_reason,
            },
        }


def _score_inputs(
    reference_features: pd.DataFrame,
    evaluation_features: pd.DataFrame,
    scorer: ReplayScorer,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(scorer, ResilientFraudScorer):
        reference_scores, evaluation_scores = scorer.score_batches(
            reference_features, evaluation_features
        )
    else:
        reference_scores = {
            "risk_probability": scorer.predict_proba(reference_features),
            "decision_score": scorer.predict_raw(reference_features),
            "decision_threshold": scorer.thresholds["precision_floor"],
            "score_space": scorer.risk_density_score_space,
            "degraded": False,
            "reason": None,
        }
        evaluation_scores = {
            "risk_probability": scorer.predict_proba(evaluation_features),
            "decision_score": scorer.predict_raw(evaluation_features),
            "decision_threshold": scorer.thresholds["precision_floor"],
            "score_space": scorer.risk_density_score_space,
            "degraded": False,
            "reason": None,
        }
    contract = ("decision_threshold", "score_space", "degraded", "reason")
    if any(reference_scores[key] != evaluation_scores[key] for key in contract):
        raise RuntimeError("Replay reference and evaluation batches must use one score contract")
    return reference_scores, evaluation_scores


def _windows(
    features: pd.DataFrame, scores: dict[str, Any], settings: Settings
) -> tuple[list[WindowSnapshot], pd.DataFrame]:
    risk = scores["risk_probability"]
    decision = scores["decision_score"]
    windows = build_sliding_windows(
        features,
        risk,
        decision,
        float(scores["decision_threshold"]),
        window_minutes=settings.detector_window_minutes,
        slide_minutes=settings.detector_slide_minutes,
    )
    return windows, features.assign(risk_probability=risk, decision_score=decision)


def replay_detector(
    evaluation_features: pd.DataFrame,
    reference_features: pd.DataFrame,
    predictor: ReplayScorer,
    settings: Settings | None = None,
) -> ReplayResult:
    settings = settings or get_settings()
    reference_scores, evaluation_scores = _score_inputs(
        reference_features, evaluation_features, predictor
    )
    reference_windows, reference_rows = _windows(reference_features, reference_scores, settings)
    evaluation_windows, _ = _windows(evaluation_features, evaluation_scores, settings)
    detector = RiskDensitySpikeDetector(settings)
    detector.prime(reference_windows)
    alerts: list[SpikeAlert] = []
    segments: dict[str, list[dict[str, object]]] = {}

    for window in evaluation_windows:
        alert = detector.process(window)
        if alert is None:
            continue
        alerts.append(alert)
        findings = discover_segments(
            window.rows,
            reference_rows,
            min_support=min(10, max(5, window.transaction_count // 4)),
            max_depth=3,
            top_k=5,
        )
        segments[alert.alert_id] = [finding.to_dict() for finding in findings]

    return ReplayResult(
        alerts=tuple(alerts),
        alert_segments=segments,
        decisions=tuple(decision.to_dict() for decision in detector.decisions),
        reference_window_count=len(reference_windows),
        evaluation_window_count=len(evaluation_windows),
        score_space=str(evaluation_scores["score_space"]),
        degraded=bool(evaluation_scores["degraded"]),
        degradation_reason=evaluation_scores["reason"],
    )
