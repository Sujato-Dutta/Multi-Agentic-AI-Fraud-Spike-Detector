"""Transaction-level evaluation for a frozen fraud model artifact."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from backend.app.ml.fraud.predictor import FraudPredictor
from evaluation.dataio import DatasetSplit
from evaluation.metrics import cost_metrics, transaction_metrics


def aligned_labels(split: DatasetSplit) -> pd.DataFrame:
    return split.features[["transaction_id"]].merge(
        split.labels, on="transaction_id", how="left", validate="one_to_one"
    )


def evaluate_fraud_model(
    split: DatasetSplit,
    predictor: FraudPredictor,
    operating_point: str = "precision_floor",
) -> dict[str, Any]:
    labels = aligned_labels(split)
    raw_scores = predictor.predict_raw(split.features)
    calibrated = predictor.predict_proba(split.features)
    threshold = predictor.thresholds[operating_point]
    predictions = (raw_scores >= threshold).astype(int)
    metrics = transaction_metrics(labels["is_fraud"], raw_scores, threshold)
    metric_values = metrics.to_dict()
    metric_values["raw_pr_auc"] = metric_values["pr_auc"]
    metric_values["raw_roc_auc"] = metric_values["roc_auc"]
    metric_values["pr_auc"] = float(average_precision_score(labels["is_fraud"], calibrated))
    metric_values["roc_auc"] = float(roc_auc_score(labels["is_fraud"], calibrated))
    costs = cost_metrics(labels, predictions, amounts=split.features["amount_inr"])
    return {
        "operating_point": operating_point,
        "threshold": threshold,
        "threshold_score_space": predictor.threshold_score_space,
        "risk_density_score_space": predictor.risk_density_score_space,
        "metrics": metric_values,
        "costs": costs.to_dict(),
        "calibrated_probability": {
            "mean": float(np.mean(calibrated)),
            "p95": float(np.quantile(calibrated, 0.95)),
            "max": float(np.max(calibrated)),
        },
    }
