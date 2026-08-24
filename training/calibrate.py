"""Probability calibration and business-aware threshold selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


@dataclass
class RankPreservingIsotonicCalibrator:
    """Isotonic probabilities with an infinitesimal raw-score tie-breaker.

    Isotonic plateaus are expected on small chronological tails. The tie-breaker preserves model
    ranking for PR-AUC while changing calibrated values by less than one part per million.
    """

    model: IsotonicRegression
    epsilon: float = 1e-6

    def predict(self, raw_probabilities: np.ndarray) -> np.ndarray:
        scores = np.asarray(raw_probabilities, dtype=float)
        calibrated = self.model.predict(scores)
        return np.clip((calibrated + self.epsilon * scores) / (1 + self.epsilon), 0.0, 1.0)


@dataclass(frozen=True)
class OperatingPoint:
    threshold: float
    precision: float
    recall: float
    f1: float
    false_positives: int
    false_negatives: int
    false_positive_cost_inr: float
    fraud_loss_missed_inr: float
    total_cost_inr: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def fit_isotonic(
    raw_probabilities: np.ndarray, labels: np.ndarray
) -> RankPreservingIsotonicCalibrator:
    scores = np.asarray(raw_probabilities, dtype=float)
    truth = np.asarray(labels, dtype=int)
    if len(scores) != len(truth) or len(scores) < 2 or len(np.unique(truth)) < 2:
        raise ValueError("Calibration requires equal-length scores with both target classes")
    model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    model.fit(scores, truth)
    return RankPreservingIsotonicCalibrator(model=model)


def threshold_curve(
    labels: np.ndarray,
    probabilities: np.ndarray,
    false_positive_costs: np.ndarray,
    fraud_losses: np.ndarray,
) -> pd.DataFrame:
    """Compute all distinct >=-threshold operating points in O(n log n)."""

    truth = np.asarray(labels, dtype=int)
    scores = np.asarray(probabilities, dtype=float)
    fp_costs = np.asarray(false_positive_costs, dtype=float)
    losses = np.asarray(fraud_losses, dtype=float)
    if not (len(truth) == len(scores) == len(fp_costs) == len(losses)) or not len(truth):
        raise ValueError("Threshold inputs must have equal non-zero length")
    if np.any((scores < 0) | (scores > 1)):
        raise ValueError("Probabilities must be within [0, 1]")

    order = np.argsort(-scores, kind="stable")
    sorted_scores = scores[order]
    sorted_truth = truth[order]
    sorted_fp_costs = fp_costs[order]
    sorted_losses = losses[order]
    tp = np.cumsum(sorted_truth == 1)
    fp = np.cumsum(sorted_truth == 0)
    fp_cost = np.cumsum(np.where(sorted_truth == 0, sorted_fp_costs, 0.0))
    captured_loss = np.cumsum(np.where(sorted_truth == 1, sorted_losses, 0.0))
    total_positives = int(truth.sum())
    total_loss = float(losses[truth == 1].sum())
    endpoint = np.r_[sorted_scores[1:] != sorted_scores[:-1], True]
    indices = np.flatnonzero(endpoint)

    rows = []
    for index in indices:
        true_positive = int(tp[index])
        false_positive = int(fp[index])
        false_negative = total_positives - true_positive
        precision = true_positive / max(true_positive + false_positive, 1)
        recall = true_positive / max(total_positives, 1)
        f1 = 2 * precision * recall / max(precision + recall, np.finfo(float).eps)
        missed_loss = total_loss - float(captured_loss[index])
        rows.append(
            {
                "threshold": float(sorted_scores[index]),
                "precision": float(precision),
                "recall": float(recall),
                "f1": float(f1),
                "false_positives": false_positive,
                "false_negatives": false_negative,
                "false_positive_cost_inr": float(fp_cost[index]),
                "fraud_loss_missed_inr": float(max(missed_loss, 0.0)),
                "total_cost_inr": float(fp_cost[index] + max(missed_loss, 0.0)),
            }
        )
    return pd.DataFrame(rows)


def select_operating_points(
    curve: pd.DataFrame, precision_floor: float = 0.90
) -> tuple[OperatingPoint, OperatingPoint]:
    required = set(OperatingPoint.__dataclass_fields__)
    missing = required.difference(curve.columns)
    if curve.empty or missing:
        raise ValueError(f"Invalid threshold curve; missing columns: {sorted(missing)}")
    eligible = curve.loc[curve["precision"].ge(precision_floor)]
    if eligible.empty:
        best_precision = float(curve["precision"].max())
        raise ValueError(
            f"No threshold satisfies precision floor {precision_floor:.3f}; best={best_precision:.3f}"
        )
    primary_row = eligible.sort_values(
        ["recall", "total_cost_inr", "threshold"], ascending=[False, True, False]
    ).iloc[0]
    cost_row = curve.sort_values(
        ["total_cost_inr", "recall", "threshold"], ascending=[True, False, False]
    ).iloc[0]
    def from_row(row: pd.Series) -> OperatingPoint:
        return OperatingPoint(
            threshold=float(row["threshold"]),
            precision=float(row["precision"]),
            recall=float(row["recall"]),
            f1=float(row["f1"]),
            false_positives=int(row["false_positives"]),
            false_negatives=int(row["false_negatives"]),
            false_positive_cost_inr=float(row["false_positive_cost_inr"]),
            fraud_loss_missed_inr=float(row["fraud_loss_missed_inr"]),
            total_cost_inr=float(row["total_cost_inr"]),
        )

    return from_row(primary_row), from_row(cost_row)
