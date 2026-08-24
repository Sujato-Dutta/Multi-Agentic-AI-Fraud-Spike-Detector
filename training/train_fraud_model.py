"""Train calibrated XGBoost risk scoring and an Isolation Forest fallback."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import joblib
import matplotlib
import mlflow
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.calibration import calibration_curve
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

matplotlib.use("Agg")
import matplotlib.pyplot as plt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.ml.fraud.features import (
    apply_category_schema,
    build_features,
    fit_category_schema,
)
from backend.app.ml.fraud.predictor import FraudPredictor
from evaluation.dataio import DatasetSplit, load_split
from training.calibrate import fit_isotonic, select_operating_points, threshold_curve


def _aligned_labels(split: DatasetSplit) -> pd.DataFrame:
    return split.features[["transaction_id"]].merge(
        split.labels, on="transaction_id", how="left", validate="one_to_one"
    )


def _xgb_parameters(scale_pos_weight: float, seed: int) -> dict[str, Any]:
    return {
        "n_estimators": 450,
        "max_depth": 5,
        "learning_rate": 0.05,
        "min_child_weight": 3,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "reg_alpha": 0.2,
        "reg_lambda": 2.0,
        "max_bin": 256,
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "tree_method": "hist",
        "enable_categorical": True,
        "scale_pos_weight": scale_pos_weight,
        "random_state": seed,
        "n_jobs": -1,
    }


def train_calibrated_bundle(
    train_features: pd.DataFrame,
    train_labels: pd.DataFrame,
    validation_features: pd.DataFrame,
    validation_labels: pd.DataFrame,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], pd.DataFrame, dict[str, Any]]:
    """Fit base model on the chronological prefix, calibrate on its tail, select on validation."""

    settings = settings or get_settings()
    if not train_features["timestamp"].is_monotonic_increasing:
        raise ValueError("Training transactions must be chronological")
    split_at = int(len(train_features) * (1 - settings.calibration_fraction))
    if split_at <= 0 or split_at >= len(train_features):
        raise ValueError("Calibration split leaves an empty partition")

    all_train_x = build_features(train_features)
    validation_x = build_features(validation_features)
    category_schema = fit_category_schema(all_train_x.iloc[:split_at])
    fit_x = apply_category_schema(all_train_x.iloc[:split_at], category_schema)
    calibration_x = apply_category_schema(all_train_x.iloc[split_at:], category_schema)
    validation_x = apply_category_schema(validation_x, category_schema)
    fit_y = train_labels["is_fraud"].to_numpy(dtype=int)[:split_at]
    calibration_y = train_labels["is_fraud"].to_numpy(dtype=int)[split_at:]
    validation_y = validation_labels["is_fraud"].to_numpy(dtype=int)
    positives = int(fit_y.sum())
    scale_pos_weight = float((len(fit_y) - positives) / max(positives, 1))

    model = xgb.XGBClassifier(**_xgb_parameters(scale_pos_weight, settings.random_seed))
    model.fit(fit_x, fit_y, eval_set=[(calibration_x, calibration_y)], verbose=False)
    calibration_raw = model.predict_proba(calibration_x)[:, 1]
    calibrator = fit_isotonic(calibration_raw, calibration_y)
    validation_raw = model.predict_proba(validation_x)[:, 1]
    validation_probabilities = np.clip(calibrator.predict(validation_raw), 0.0, 1.0)

    curve = threshold_curve(
        validation_y,
        validation_raw,
        validation_labels["false_positive_cost_if_blocked_inr"].to_numpy(float),
        validation_labels["fraud_loss_if_missed_inr"].to_numpy(float),
    )
    primary, cost_optimal = select_operating_points(
        curve, settings.validation_precision_floor
    )
    metrics = {
        "validation_pr_auc": float(
            average_precision_score(validation_y, validation_probabilities)
        ),
        "validation_raw_pr_auc": float(average_precision_score(validation_y, validation_raw)),
        "validation_calibrated_pr_auc": float(
            average_precision_score(validation_y, validation_probabilities)
        ),
        "validation_roc_auc": float(roc_auc_score(validation_y, validation_probabilities)),
        "validation_raw_roc_auc": float(roc_auc_score(validation_y, validation_raw)),
        "validation_brier_score": float(brier_score_loss(validation_y, validation_probabilities)),
        "fit_rows": len(fit_x),
        "calibration_rows": len(calibration_x),
        "validation_rows": len(validation_x),
        "fit_end_timestamp": train_features.iloc[split_at - 1]["timestamp"].isoformat(),
        "calibration_start_timestamp": train_features.iloc[split_at]["timestamp"].isoformat(),
        "precision_floor": primary.to_dict(),
        "cost_optimal": cost_optimal.to_dict(),
        "decision_threshold_score_space": "raw_xgboost_probability",
        "risk_density_score_space": "rank_preserving_isotonic_probability",
    }
    bundle = {
        "artifact_version": 1,
        "model": model,
        "calibrator": calibrator,
        "category_schema": category_schema,
        "feature_columns": list(all_train_x.columns),
        "thresholds": {
            "precision_floor": primary.threshold,
            "cost_optimal": cost_optimal.threshold,
        },
        "threshold_score_space": "raw_xgboost_probability",
        "risk_density_score_space": "rank_preserving_isotonic_probability",
        "metadata": metrics,
    }
    return bundle, curve, metrics


def train_anomaly_fallback(
    train: DatasetSplit, model_dir: Path, settings: Settings
) -> dict[str, Any]:
    labels = _aligned_labels(train)
    normal = labels["is_within_spike_window"].eq(0).to_numpy()
    features = build_features(train.features.loc[normal])
    encoded = pd.get_dummies(features, columns=list(features.select_dtypes("category").columns))
    estimator = IsolationForest(
        n_estimators=250,
        contamination="auto",
        max_samples=min(4096, len(encoded)),
        random_state=settings.random_seed,
        n_jobs=-1,
    )
    estimator.fit(encoded)
    training_anomaly_scores = np.sort(-estimator.decision_function(encoded))
    artifact = {
        "artifact_version": 1,
        "model": estimator,
        "encoded_columns": list(encoded.columns),
        "feature_columns": list(features.columns),
        "reference_anomaly_scores": training_anomaly_scores,
        "risk_score_space": "empirical_tail_severity_0_1",
        "decision_threshold": 0.5,
        "training_rows": len(encoded),
        "purpose": "fallback_support_signal_only",
    }
    path = model_dir / "isolation_forest.joblib"
    joblib.dump(artifact, path)
    return {"path": str(path), "training_rows": len(encoded)}


def _save_diagnostics(
    model: xgb.XGBClassifier,
    curve: pd.DataFrame,
    output_dir: Path,
    validation_y: np.ndarray,
    raw_probabilities: np.ndarray,
    calibrated_probabilities: np.ndarray,
) -> tuple[Path, Path, Path]:
    curve_path = output_dir / "validation_threshold_curve.csv"
    curve.to_csv(curve_path, index=False)
    importance = pd.Series(
        model.feature_importances_, index=model.feature_names_in_, name="importance"
    ).sort_values(ascending=False).head(20)
    importance_path = output_dir / "feature_importance.png"
    fig, ax = plt.subplots(figsize=(9, 6))
    importance.sort_values().plot.barh(ax=ax, color="#4b8bf5")
    ax.set(title="XGBoost feature importance", xlabel="Gain proxy")
    fig.tight_layout()
    fig.savefig(importance_path, dpi=160)
    plt.close(fig)

    calibration_path = output_dir / "calibration_curve.png"
    fig, ax = plt.subplots(figsize=(6, 6))
    for label, probabilities, color in (
        ("Raw XGBoost", raw_probabilities, "#f59e0b"),
        ("Isotonic", calibrated_probabilities, "#4b8bf5"),
    ):
        observed, predicted = calibration_curve(
            validation_y, probabilities, n_bins=10, strategy="quantile"
        )
        ax.plot(predicted, observed, marker="o", label=label, color=color)
    ax.plot([0, 1], [0, 1], linestyle="--", color="#64748b", label="Ideal")
    ax.set(xlabel="Mean predicted probability", ylabel="Observed fraud rate", title="Validation calibration")
    ax.legend()
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(calibration_path, dpi=160)
    plt.close(fig)
    return curve_path, importance_path, calibration_path


def train_and_save(include_anomaly: bool = True) -> dict[str, Any]:
    settings = get_settings()
    train = load_split("train")
    validation = load_split("validation")
    train_labels = _aligned_labels(train)
    validation_labels = _aligned_labels(validation)
    model_dir = settings.model_dir / "fraud"
    model_dir.mkdir(parents=True, exist_ok=True)
    settings.report_dir.joinpath("metrics").mkdir(parents=True, exist_ok=True)

    bundle, curve, metrics = train_calibrated_bundle(
        train.features, train_labels, validation.features, validation_labels, settings
    )
    artifact_path = model_dir / "fraud_model.joblib"
    joblib.dump(bundle, artifact_path)
    metadata_path = model_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    predictor = FraudPredictor(bundle)
    validation_raw = predictor.predict_raw(validation.features)
    validation_calibrated = predictor.predict_proba(validation.features)
    curve_path, importance_path, calibration_path = _save_diagnostics(
        bundle["model"],
        curve,
        model_dir,
        validation_labels["is_fraud"].to_numpy(dtype=int),
        validation_raw,
        validation_calibrated,
    )
    anomaly = train_anomaly_fallback(train, model_dir, settings) if include_anomaly else None

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("fraud-transaction-risk")
    with mlflow.start_run(run_name="xgboost-isotonic-validation"):
        mlflow.log_params(_xgb_parameters(bundle["model"].scale_pos_weight, settings.random_seed))
        mlflow.log_metrics(
            {
                "validation_pr_auc": metrics["validation_pr_auc"],
                "validation_raw_pr_auc": metrics["validation_raw_pr_auc"],
                "validation_calibrated_pr_auc": metrics["validation_calibrated_pr_auc"],
                "validation_roc_auc": metrics["validation_roc_auc"],
                "validation_raw_roc_auc": metrics["validation_raw_roc_auc"],
                "validation_brier_score": metrics["validation_brier_score"],
                "precision_floor_precision": metrics["precision_floor"]["precision"],
                "precision_floor_recall": metrics["precision_floor"]["recall"],
                "precision_floor_fp_cost_inr": metrics["precision_floor"]["false_positive_cost_inr"],
                "cost_optimal_total_cost_inr": metrics["cost_optimal"]["total_cost_inr"],
            }
        )
        for artifact in (
            artifact_path,
            metadata_path,
            curve_path,
            importance_path,
            calibration_path,
        ):
            mlflow.log_artifact(str(artifact), artifact_path="fraud_model")
        if anomaly:
            mlflow.log_artifact(anomaly["path"], artifact_path="fraud_model")

    result = {"artifact": str(artifact_path), "metrics": metrics, "anomaly": anomaly}
    report_path = settings.report_dir / "metrics" / "fraud_validation.json"
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-anomaly", action="store_true", help="Skip Isolation Forest fallback training"
    )
    args = parser.parse_args()
    print(json.dumps(train_and_save(include_anomaly=not args.no_anomaly), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
