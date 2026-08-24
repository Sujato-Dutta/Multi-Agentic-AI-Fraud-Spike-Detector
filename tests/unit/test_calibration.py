from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

import joblib
import numpy as np
import pytest

from backend.app.ml.fraud.predictor import (
    AnomalyPredictor,
    FraudPredictor,
    ResilientFraudScorer,
)
from evaluation.dataio import load_split
from evaluation.replay import replay_detector
from training.calibrate import fit_isotonic, select_operating_points, threshold_curve


def test_isotonic_calibration_is_bounded_and_monotone() -> None:
    raw = np.array([0.01, 0.08, 0.12, 0.35, 0.60, 0.90])
    labels = np.array([0, 0, 1, 0, 1, 1])
    calibrator = fit_isotonic(raw, labels)
    grid = np.linspace(0.0, 1.0, 100)
    calibrated = calibrator.predict(grid)

    assert np.all(np.diff(calibrated) > 0)
    assert calibrated.min() >= 0 and calibrated.max() <= 1


def test_threshold_selector_honours_precision_floor_and_cost_objective() -> None:
    labels = np.array([1] * 9 + [0, 1, 0, 0])
    probabilities = np.array([0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.91, 0.90, 0.89, 0.3, 0.1])
    fp_costs = np.array([0] * 9 + [100, 0, 10, 10], dtype=float)
    fraud_losses = np.array([500] * 9 + [0, 1000, 0, 0], dtype=float)
    curve = threshold_curve(labels, probabilities, fp_costs, fraud_losses)
    primary, cost_optimal = select_operating_points(curve, precision_floor=0.90)
    eligible = curve.loc[curve["precision"].ge(0.90)]

    assert primary.precision >= 0.90
    assert primary.recall == eligible["recall"].max()
    assert cost_optimal.total_cost_inr == curve["total_cost_inr"].min()
    assert cost_optimal.recall >= primary.recall


def test_threshold_selector_fails_closed_when_floor_is_impossible() -> None:
    curve = threshold_curve(
        np.array([0, 1, 0]),
        np.array([0.9, 0.8, 0.7]),
        np.array([10.0, 0.0, 10.0]),
        np.array([0.0, 100.0, 0.0]),
    )
    with pytest.raises(ValueError, match="No threshold satisfies"):
        select_operating_points(curve, precision_floor=0.90)


def test_model_artifact_contracts_reject_incomplete_and_wrong_score_spaces() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        FraudPredictor({})
    invalid = {
        "artifact_version": 1,
        "model": object(),
        "calibrator": object(),
        "category_schema": {},
        "feature_columns": [],
        "thresholds": {},
        "threshold_score_space": "calibrated_probability",
        "risk_density_score_space": "rank_preserving_isotonic_probability",
    }
    with pytest.raises(ValueError, match="raw_xgboost_probability"):
        FraudPredictor(invalid)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        anomaly_artifact = joblib.load("models/fraud/isolation_forest.joblib")
    missing_threshold = dict(anomaly_artifact)
    missing_threshold.pop("decision_threshold")
    with pytest.raises(ValueError, match="incomplete"):
        AnomalyPredictor(missing_threshold)
    wrong_space = dict(anomaly_artifact, risk_score_space="raw_isolation_score")
    with pytest.raises(ValueError, match="empirical_tail_severity_0_1"):
        AnomalyPredictor(wrong_space)


def test_primary_load_or_scoring_failure_uses_one_bounded_fallback_space(tmp_path) -> None:
    fallback_path = Path("models/fraud/isolation_forest.joblib")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        scorer = ResilientFraudScorer(tmp_path / "missing-primary.joblib", fallback_path)
    validation = load_split("validation").features
    scores = scorer.score(validation.head(20))
    assert scorer.degraded and scores["degraded"]
    assert np.all((scores["risk_probability"] >= 0) & (scores["risk_probability"] <= 1))
    assert scores["decision_threshold"] == 0.5
    assert scores["score_space"] == "empirical_tail_severity_0_1"
    replay = replay_detector(
        validation.iloc[:200], load_split("train").features.iloc[:800], scorer
    )
    assert replay.degraded
    assert replay.score_space == "empirical_tail_severity_0_1"
    assert replay.degradation_reason == "primary_fraud_model_unavailable:FileNotFoundError"

    broken_primary = {
        "artifact_version": 1,
        "model": object(),
        "calibrator": object(),
        "category_schema": {},
        "feature_columns": [],
        "thresholds": {"precision_floor": 0.5},
        "threshold_score_space": "raw_xgboost_probability",
        "risk_density_score_space": "rank_preserving_isotonic_probability",
    }
    broken_path = tmp_path / "broken-primary.joblib"
    joblib.dump(broken_primary, broken_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        atomic_scorer = ResilientFraudScorer(broken_path, fallback_path)
        reference_scores, evaluation_scores = atomic_scorer.score_batches(
            validation.iloc[:20], validation.iloc[20:40]
        )
    assert atomic_scorer.degraded
    assert atomic_scorer.degradation_reason == "primary_fraud_model_scoring_failed:AttributeError"
    assert reference_scores["score_space"] == evaluation_scores["score_space"]
    assert reference_scores["decision_threshold"] == evaluation_scores["decision_threshold"]
    assert reference_scores["degraded"] and evaluation_scores["degraded"]


def test_phase2_report_has_named_score_metrics_and_model_provenance() -> None:
    report = json.loads(Path("reports/metrics/phase2_benchmark.json").read_text(encoding="utf-8"))
    provenance = report["model_artifact"]
    model_path = Path(provenance["path"])
    metrics = report["validation"]["transaction"]["precision_floor"]["metrics"]

    assert {"pr_auc", "raw_pr_auc", "roc_auc", "raw_roc_auc"}.issubset(metrics)
    assert provenance["threshold_score_space"] == "raw_xgboost_probability"
    assert provenance["risk_density_score_space"] == "rank_preserving_isotonic_probability"
    assert provenance["sha256"] == hashlib.sha256(model_path.read_bytes()).hexdigest()
    assert report["validation"]["spikes"]["scoring"] == {
        "score_space": "rank_preserving_isotonic_probability",
        "degraded": False,
        "reason": None,
    }
