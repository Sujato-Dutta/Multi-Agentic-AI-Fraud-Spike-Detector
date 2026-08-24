"""Artifact-backed transaction risk predictor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from backend.app.ml.fraud.features import apply_category_schema, build_features

_RULE_WEIGHTS = {
    "proxy_ip": 0.75,
    "repeated_failures": 0.75,
    "new_risky_device": 0.70,
    "account_changes": 0.65,
    "high_velocity": 0.65,
    "prior_disputes": 0.60,
    "address_mismatch": 0.55,
}
_RULE_DECISION_THRESHOLD = 0.50


class FraudPredictor:
    """Load and run the exact model/calibration feature path used during training."""

    def __init__(self, artifact: dict[str, Any]) -> None:
        required = {
            "artifact_version",
            "model",
            "calibrator",
            "category_schema",
            "feature_columns",
            "thresholds",
            "threshold_score_space",
            "risk_density_score_space",
        }
        missing = required.difference(artifact)
        if missing:
            raise ValueError(f"Fraud model artifact is incomplete: {sorted(missing)}")
        if artifact["artifact_version"] != 1:
            raise ValueError(f"Unsupported fraud artifact version: {artifact['artifact_version']}")
        if artifact["threshold_score_space"] != "raw_xgboost_probability":
            raise ValueError("Fraud thresholds must use raw_xgboost_probability")
        if artifact["risk_density_score_space"] != "rank_preserving_isotonic_probability":
            raise ValueError("Fraud risk density must use rank_preserving_isotonic_probability")
        self._artifact = artifact

    @classmethod
    def load(cls, path: Path | str) -> FraudPredictor:
        return cls(joblib.load(path))

    @property
    def thresholds(self) -> dict[str, float]:
        return dict(self._artifact["thresholds"])

    @property
    def threshold_score_space(self) -> str:
        return str(self._artifact["threshold_score_space"])

    @property
    def risk_density_score_space(self) -> str:
        return str(self._artifact["risk_density_score_space"])

    def predict_raw(self, transactions: pd.DataFrame) -> np.ndarray:
        features = build_features(transactions)
        expected = self._artifact["feature_columns"]
        missing = set(expected).difference(features.columns)
        if missing:
            raise ValueError(f"Scoring input is missing model features: {sorted(missing)}")
        features = apply_category_schema(features[expected], self._artifact["category_schema"])
        return self._artifact["model"].predict_proba(features)[:, 1]

    def predict_proba(self, transactions: pd.DataFrame) -> np.ndarray:
        raw = self.predict_raw(transactions)
        return np.clip(self._artifact["calibrator"].predict(raw), 0.0, 1.0)

    def predict(self, transactions: pd.DataFrame, operating_point: str = "precision_floor") -> np.ndarray:
        threshold = self.thresholds[operating_point]
        return (self.predict_raw(transactions) >= threshold).astype(int)


class AnomalyPredictor:
    """Isolation Forest support signal used when primary scoring is degraded."""

    def __init__(self, artifact: dict[str, Any]) -> None:
        required = {
            "artifact_version",
            "model",
            "feature_columns",
            "encoded_columns",
            "reference_anomaly_scores",
            "risk_score_space",
            "decision_threshold",
        }
        missing = required.difference(artifact)
        if missing:
            raise ValueError(f"Anomaly artifact is incomplete: {sorted(missing)}")
        if artifact["artifact_version"] != 1:
            raise ValueError(f"Unsupported anomaly artifact version: {artifact['artifact_version']}")
        if artifact["risk_score_space"] != "empirical_tail_severity_0_1":
            raise ValueError("Anomaly risk scores must use empirical_tail_severity_0_1")
        reference = np.asarray(artifact["reference_anomaly_scores"], dtype=float)
        if reference.ndim != 1 or not len(reference) or np.any(np.diff(reference) < 0):
            raise ValueError("Anomaly reference scores must be a non-empty sorted vector")
        if not 0.0 <= float(artifact["decision_threshold"]) <= 1.0:
            raise ValueError("Anomaly decision threshold must be within [0, 1]")
        self._artifact = artifact

    @classmethod
    def load(cls, path: Path | str) -> AnomalyPredictor:
        return cls(joblib.load(path))

    @property
    def decision_threshold(self) -> float:
        return float(self._artifact["decision_threshold"])

    @property
    def risk_score_space(self) -> str:
        return str(self._artifact["risk_score_space"])

    def raw_anomaly_score(self, transactions: pd.DataFrame) -> np.ndarray:
        features = build_features(transactions)
        expected = self._artifact["feature_columns"]
        missing = set(expected).difference(features.columns)
        if missing:
            raise ValueError(f"Scoring input is missing fallback features: {sorted(missing)}")
        encoded = pd.get_dummies(
            features[expected], columns=list(features[expected].select_dtypes("category").columns)
        ).reindex(columns=self._artifact["encoded_columns"], fill_value=0)
        return -self._artifact["model"].decision_function(encoded)

    def anomaly_score(self, transactions: pd.DataFrame) -> np.ndarray:
        """Return a bounded empirical tail-severity signal compatible with detector windows."""

        raw = self.raw_anomaly_score(transactions)
        reference = np.asarray(self._artifact["reference_anomaly_scores"], dtype=float)
        percentiles = np.searchsorted(reference, raw, side="right") / len(reference)
        return np.clip((percentiles - 0.90) / 0.10, 0.0, 1.0)


class ResilientFraudScorer:
    """Primary scorer with a bounded, explicit degraded-mode anomaly fallback."""

    def __init__(self, primary_path: Path | str, fallback_path: Path | str) -> None:
        self.primary: FraudPredictor | None = None
        self.fallback: AnomalyPredictor | None = None
        self.degradation_reason: str | None = None
        self._fallback_path = fallback_path
        try:
            self.primary = FraudPredictor.load(primary_path)
        except Exception as error:  # noqa: BLE001 - artifact corruption must degrade, not crash open
            self._activate_fallback(f"primary_fraud_model_unavailable:{type(error).__name__}")

    def _activate_fallback(self, reason: str) -> None:
        self.primary = None
        if self.fallback is None:
            try:
                self.fallback = AnomalyPredictor.load(self._fallback_path)
            except Exception as error:  # noqa: BLE001 - deterministic rules remain available
                reason = f"{reason}:anomaly_fallback_unavailable:{type(error).__name__}"
        self.degradation_reason = reason

    @property
    def degraded(self) -> bool:
        return self.primary is None

    def _primary_score(self, transactions: pd.DataFrame) -> dict[str, Any]:
        assert self.primary is not None
        return {
            "risk_probability": self.primary.predict_proba(transactions),
            "decision_score": self.primary.predict_raw(transactions),
            "decision_threshold": self.primary.thresholds["precision_floor"],
            "score_space": self.primary.risk_density_score_space,
            "degraded": False,
            "reason": None,
        }

    def _fallback_score(self, transactions: pd.DataFrame) -> dict[str, Any]:
        if self.fallback is None:
            severity = _conservative_rule_score(transactions)
            threshold = _RULE_DECISION_THRESHOLD
            score_space = "deterministic_conservative_rule_score"
            reason = f"{self.degradation_reason}:conservative_rules_active"
        else:
            severity = self.fallback.anomaly_score(transactions)
            threshold = self.fallback.decision_threshold
            score_space = self.fallback.risk_score_space
            reason = self.degradation_reason
        return {
            "risk_probability": severity,
            "decision_score": severity,
            "decision_threshold": threshold,
            "score_space": score_space,
            "degraded": True,
            "reason": reason,
        }

    def score_batches(self, *transaction_batches: pd.DataFrame) -> tuple[dict[str, Any], ...]:
        """Score related batches atomically so a replay cannot mix primary and fallback spaces."""

        if not transaction_batches:
            return ()
        if self.primary is not None:
            try:
                return tuple(self._primary_score(batch) for batch in transaction_batches)
            except Exception as error:  # noqa: BLE001 - score failures activate visible degradation
                self._activate_fallback(f"primary_fraud_model_scoring_failed:{type(error).__name__}")
        return tuple(self._fallback_score(batch) for batch in transaction_batches)

    def score(self, transactions: pd.DataFrame) -> dict[str, Any]:
        return self.score_batches(transactions)[0]


def _conservative_rule_score(transactions: pd.DataFrame) -> np.ndarray:
    """Transparent fail-safe risk floor when learned artifacts are unavailable."""

    count = len(transactions)
    score = np.zeros(count, dtype=float)

    def values(name: str, default: float = 0.0) -> np.ndarray:
        if name not in transactions:
            return np.full(count, default, dtype=float)
        return pd.to_numeric(transactions[name], errors="coerce").fillna(default).to_numpy()

    score = np.maximum(score, (values("is_proxy_ip") > 0) * _RULE_WEIGHTS["proxy_ip"])
    score = np.maximum(
        score, (values("failed_attempts_24h") >= 3) * _RULE_WEIGHTS["repeated_failures"]
    )
    score = np.maximum(
        score,
        (
            (values("is_new_device") > 0)
            & (values("ip_risk_score") >= 0.5)
        )
        * _RULE_WEIGHTS["new_risky_device"],
    )
    score = np.maximum(
        score, (values("account_changes_24h") >= 2) * _RULE_WEIGHTS["account_changes"]
    )
    score = np.maximum(
        score, (values("txn_velocity_1h") >= 10) * _RULE_WEIGHTS["high_velocity"]
    )
    score = np.maximum(
        score, (values("prior_disputes_90d") >= 2) * _RULE_WEIGHTS["prior_disputes"]
    )
    score = np.maximum(
        score,
        (values("billing_shipping_mismatch") > 0) * _RULE_WEIGHTS["address_mismatch"],
    )
    return np.clip(score, 0.0, 1.0)
