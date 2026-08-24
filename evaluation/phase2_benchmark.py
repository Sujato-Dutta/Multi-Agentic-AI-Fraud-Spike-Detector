"""Generate the Phase 2 validation and chronological train-tail benchmark."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import mlflow
import pandas as pd

from backend.app.config import Settings, get_settings
from backend.app.ml.fraud.predictor import FraudPredictor
from evaluation.dataio import DatasetSplit, load_benign_events, load_split
from evaluation.evaluate_business_cost import evaluate_business_cost
from evaluation.evaluate_fraud import aligned_labels, evaluate_fraud_model
from evaluation.evaluate_spikes import evaluate_spike_replay
from evaluation.metrics import CostMetrics
from evaluation.replay import replay_detector
from training.train_fraud_model import train_calibrated_bundle

DEV_TEST_START = pd.Timestamp("2026-05-11 00:00:00")


def _subset_split(split: DatasetSplit, mask: pd.Series) -> DatasetSplit:
    features = split.features.loc[mask].reset_index(drop=True)
    labels = features[["transaction_id"]].merge(
        split.labels, on="transaction_id", how="left", validate="one_to_one"
    )
    return DatasetSplit(features=features, labels=labels, spike_events=split.spike_events.copy())


def _temporary_dev_predictor(
    train: DatasetSplit, settings: Settings
) -> tuple[FraudPredictor, DatasetSplit, DatasetSplit]:
    pre_mask = train.features["timestamp"].lt(DEV_TEST_START)
    dev_mask = ~pre_mask
    pre = _subset_split(train, pre_mask)
    dev = _subset_split(train, dev_mask)
    tune_at = int(len(pre.features) * 0.80)
    fit_features = pre.features.iloc[:tune_at].reset_index(drop=True)
    tune_features = pre.features.iloc[tune_at:].reset_index(drop=True)
    pre_labels = aligned_labels(pre)
    fit_labels = pre_labels.iloc[:tune_at].reset_index(drop=True)
    tune_labels = pre_labels.iloc[tune_at:].reset_index(drop=True)
    bundle, _, _ = train_calibrated_bundle(
        fit_features, fit_labels, tune_features, tune_labels, settings
    )
    return FraudPredictor(bundle), pre, dev


def _business_from_result(result: dict[str, Any], reviewed_incidents: int) -> dict[str, Any]:
    costs = CostMetrics(**result["costs"])
    return evaluate_business_cost(costs, reviewed_incidents=reviewed_incidents)


def _markdown(report: dict[str, Any]) -> str:
    txn = report["validation"]["transaction"]["precision_floor"]
    spike = report["validation"]["spikes"]["metrics"]
    dev = report["dev_test"]["spikes"]["metrics"]
    return f"""# Phase 2 Benchmark

This is a development benchmark, **not the held-out test result**. Validation is used for model and
detector selection. The train-tail dev-test model is fit/tuned only on transactions before
`{report['dev_test']['cutoff']}`.

## Validation transaction model

| Metric | Value |
|---|---:|
| Calibrated PR-AUC | {txn['metrics']['pr_auc']:.4f} |
| Raw-score PR-AUC | {txn['metrics']['raw_pr_auc']:.4f} |
| Calibrated ROC-AUC | {txn['metrics']['roc_auc']:.4f} |
| Precision | {txn['metrics']['precision']:.4f} |
| Recall | {txn['metrics']['recall']:.4f} |
| F1 | {txn['metrics']['f1']:.4f} |
| False-positive cost | ₹{txn['costs']['false_positive_cost_inr']:,.2f} |
| Fraud loss missed | ₹{txn['costs']['fraud_loss_missed_inr']:,.2f} |
| Fraud exposure captured | ₹{txn['costs']['fraud_exposure_captured_inr']:,.2f} |

Raw XGBoost probabilities supply the transaction operating point. Rank-preserving isotonic
probabilities supply the aggregate risk-density signal and the protocol PR-AUC/ROC-AUC; raw ranking
metrics are also reported explicitly. The score spaces are validated by the model artifact.

## Validation spike detector

| Metric | Value |
|---|---:|
| Event precision | {spike['precision']:.4f} |
| Event recall | {spike['recall']:.4f} |
| Matched events | {spike['matched_events']} / {spike['total_events']} |
| False alerts | {spike['false_alerts']} |
| False alerts in benign surge | {spike['benign_window_false_alerts']} |
| Median detection delay | {spike['median_delay_minutes']:.1f} min |
| P90 detection delay | {spike['p90_delay_minutes']:.1f} min |

## Chronological train-tail dev-test

| Metric | Value |
|---|---:|
| Event precision | {dev['precision']:.4f} |
| Event recall | {dev['recall']:.4f} |
| Matched events | {dev['matched_events']} / {dev['total_events']} |
| False alerts | {dev['false_alerts']} |
| False alerts in `TRN_B2` | {dev['benign_window_false_alerts']} |
| Median detection delay | {dev['median_delay_minutes']:.1f} min |

## Integrity

- No raw held-out test labels or test spike events were loaded.
- Volume lift is emitted only as context and does not participate in the trigger predicate.
- Promotion context raises the required density lift and never suppresses a qualifying spike.
- Financial values are synthetic proxies; see `reports/COST_ASSUMPTIONS.md`.
"""


def run_phase2_benchmark(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    train = load_split("train")
    validation = load_split("validation")
    model_artifact = settings.model_dir / "fraud" / "fraud_model.joblib"
    final_predictor = FraudPredictor.load(model_artifact)

    validation_transactions = {
        point: evaluate_fraud_model(validation, final_predictor, point)
        for point in ("precision_floor", "cost_optimal")
    }
    validation_replay = replay_detector(
        validation.features, train.features, final_predictor, settings
    )
    validation_spikes = evaluate_spike_replay(
        validation_replay,
        validation.spike_events,
        load_benign_events("validation"),
        stream_start=validation.features["timestamp"].min(),
        stream_end=validation.features["timestamp"].max(),
        grace_minutes=settings.event_match_grace_minutes,
    )
    validation_business = _business_from_result(
        validation_transactions["precision_floor"], len(validation_replay.alerts)
    )

    dev_predictor, pre_dev, dev = _temporary_dev_predictor(train, settings)
    dev_events = train.spike_events.loc[
        train.spike_events["start_timestamp"].ge(DEV_TEST_START)
    ].reset_index(drop=True)
    dev_replay = replay_detector(dev.features, pre_dev.features, dev_predictor, settings)
    dev_spikes = evaluate_spike_replay(
        dev_replay,
        dev_events,
        load_benign_events("train").loc[
            lambda frame: frame["start_timestamp"].ge(DEV_TEST_START)
        ].reset_index(drop=True),
        stream_start=dev.features["timestamp"].min(),
        stream_end=dev.features["timestamp"].max(),
        grace_minutes=settings.event_match_grace_minutes,
    )

    report: dict[str, Any] = {
        "report_type": "phase2_development_benchmark",
        "heldout_test_accessed": False,
        "model_artifact": {
            "path": str(model_artifact),
            "sha256": hashlib.sha256(model_artifact.read_bytes()).hexdigest(),
            "threshold_score_space": final_predictor.threshold_score_space,
            "risk_density_score_space": final_predictor.risk_density_score_space,
        },
        "detector_config": {
            "window_minutes": settings.detector_window_minutes,
            "slide_minutes": settings.detector_slide_minutes,
            "min_support": settings.detector_min_support,
            "min_high_risk_count": settings.detector_min_high_risk_count,
            "density_lift": settings.detector_lift_threshold,
            "extreme_lift": settings.detector_extreme_lift,
            "alpha": settings.detector_alpha,
            "confirmation_windows": settings.detector_confirm_windows,
            "promo_lift_margin": settings.detector_promo_lift_margin,
        },
        "validation": {
            "transaction": validation_transactions,
            "business": validation_business,
            "spikes": validation_spikes,
        },
        "dev_test": {
            "cutoff": DEV_TEST_START.isoformat(),
            "fit_and_tune_rows": len(pre_dev.features),
            "evaluation_rows": len(dev.features),
            "spikes": dev_spikes,
        },
    }
    output_dir = settings.report_dir / "metrics"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase2_benchmark.json"
    markdown_path = output_dir / "phase2_benchmark.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("fraud-spike-detector")
    with mlflow.start_run(run_name="phase2-validation-replay"):
        metrics = validation_spikes["metrics"]
        mlflow.log_params(report["detector_config"])
        mlflow.log_metrics(
            {
                "validation_event_precision": metrics["precision"],
                "validation_event_recall": metrics["recall"],
                "validation_median_delay_minutes": metrics["median_delay_minutes"] or 0,
                "validation_false_alerts": metrics["false_alerts"],
                "validation_benign_false_alerts": metrics["benign_window_false_alerts"],
            }
        )
        mlflow.log_artifact(str(json_path), artifact_path="benchmark")
        mlflow.log_artifact(str(markdown_path), artifact_path="benchmark")
    return report


if __name__ == "__main__":
    print(json.dumps(run_phase2_benchmark(), indent=2))
