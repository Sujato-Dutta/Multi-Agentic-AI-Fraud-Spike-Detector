"""Advisory distribution drift. Drift raises alerts; it never changes a model or policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select

from backend.app.config import Settings, get_settings
from backend.app.db.models import FraudScore
from backend.app.monitoring.prometheus import DRIFT_ALERT_ACTIVE, DRIFT_PSI

# Only the calibrated score is observable from the live scores table, so only the calibrated
# score is monitored. Advertising feature-level drift we cannot measure would read as
# "measured and stable" when it is really "never observed".
MONITORED_FEATURES: tuple[str, ...] = ("risk_probability",)


@dataclass(frozen=True, slots=True)
class DriftResult:
    feature: str
    psi: float
    reference_count: int
    observed_count: int
    alert: bool
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "psi": self.psi,
            "reference_count": self.reference_count,
            "observed_count": self.observed_count,
            "alert": self.alert,
            "reason": self.reason,
        }


def population_stability_index(
    reference: Sequence[float], observed: Sequence[float], *, buckets: int = 10
) -> float:
    """Quantile-bucketed PSI with Laplace smoothing so empty buckets stay finite."""

    reference_values = np.asarray([v for v in reference if np.isfinite(v)], dtype=float)
    observed_values = np.asarray([v for v in observed if np.isfinite(v)], dtype=float)
    if len(reference_values) < buckets or len(observed_values) < buckets:
        raise ValueError("PSI requires at least one observation per bucket in both samples")
    quantiles = np.unique(
        np.quantile(reference_values, np.linspace(0.0, 1.0, buckets + 1))
    )
    if len(quantiles) < 3:
        return 0.0
    edges = np.concatenate(([-np.inf], quantiles[1:-1], [np.inf]))
    reference_counts = np.histogram(reference_values, bins=edges)[0].astype(float)
    observed_counts = np.histogram(observed_values, bins=edges)[0].astype(float)
    reference_share = (reference_counts + 1.0) / (reference_counts.sum() + len(edges) - 1)
    observed_share = (observed_counts + 1.0) / (observed_counts.sum() + len(edges) - 1)
    return float(np.sum((observed_share - reference_share) * np.log(observed_share / reference_share)))


class DriftMonitor:
    """Compare a rolling window of live scores against the frozen training reference."""

    def __init__(
        self,
        *,
        session_factory: Any,
        reference: dict[str, np.ndarray],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.reference = reference
        self.settings = settings or get_settings()
        self.results: tuple[DriftResult, ...] = ()

    @classmethod
    def from_training_reference(
        cls,
        *,
        session_factory: Any,
        reference_frame: Any,
        risk_probabilities: Sequence[float],
        settings: Settings | None = None,
    ) -> DriftMonitor:
        reference: dict[str, np.ndarray] = {
            "risk_probability": np.asarray(risk_probabilities, dtype=float)
        }
        for name in MONITORED_FEATURES:
            if name != "risk_probability" and name in reference_frame:
                reference[name] = reference_frame[name].to_numpy(dtype=float)
        return cls(session_factory=session_factory, reference=reference, settings=settings)

    async def refresh(self) -> tuple[DriftResult, ...]:
        """Recompute PSI from the newest scored transactions and publish advisory gauges."""

        threshold = self.settings.drift_psi_alert_threshold
        window = self.settings.drift_window_transactions
        observed = await self._recent_scores(window)
        results: list[DriftResult] = []
        for feature, reference_values in self.reference.items():
            observed_values = observed.get(feature, np.asarray([], dtype=float))
            try:
                psi = population_stability_index(
                    reference_values, observed_values, buckets=self.settings.drift_psi_buckets
                )
            except ValueError as exc:
                results.append(
                    DriftResult(
                        feature=feature,
                        psi=0.0,
                        reference_count=len(reference_values),
                        observed_count=len(observed_values),
                        alert=False,
                        reason=f"insufficient_observations: {exc}",
                    )
                )
                DRIFT_ALERT_ACTIVE.labels(feature=feature).set(0)
                continue
            alert = psi >= threshold
            DRIFT_PSI.labels(feature=feature).set(psi)
            DRIFT_ALERT_ACTIVE.labels(feature=feature).set(1 if alert else 0)
            results.append(
                DriftResult(
                    feature=feature,
                    psi=psi,
                    reference_count=len(reference_values),
                    observed_count=len(observed_values),
                    alert=alert,
                    reason="psi_above_threshold" if alert else None,
                )
            )
        self.results = tuple(results)
        return self.results

    async def _recent_scores(self, window: int) -> dict[str, np.ndarray]:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(FraudScore.risk_probability)
                        .order_by(FraudScore.scored_at.desc(), FraudScore.score_id.desc())
                        .limit(window)
                    )
                ).all()
            )
        return {"risk_probability": np.asarray(rows, dtype=float)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "psi_alert_threshold": self.settings.drift_psi_alert_threshold,
            "window_transactions": self.settings.drift_window_transactions,
            "auto_retrain": False,
            "auto_policy_change": False,
            "features": [result.to_dict() for result in self.results],
        }
