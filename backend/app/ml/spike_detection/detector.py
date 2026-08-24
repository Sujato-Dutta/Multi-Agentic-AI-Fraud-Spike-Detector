"""Stateful risk-density spike detection with robust adaptive baselines."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import timedelta
from math import exp, log

import numpy as np
import pandas as pd
from scipy.stats import poisson

from backend.app.config import Settings, get_settings
from backend.app.ml.spike_detection.windows import WindowSnapshot


@dataclass(frozen=True)
class SpikeAlert:
    alert_id: str
    fire_timestamp: pd.Timestamp
    window_start: pd.Timestamp
    transaction_count: int
    risk_density: float
    baseline_density: float
    density_lift: float
    high_risk_count: int
    expected_high_risk_rate: float
    p_value: float
    volume_lift: float
    promo_share: float
    required_lift: float
    reason: str
    drift_psi: float

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["fire_timestamp"] = self.fire_timestamp.isoformat()
        result["window_start"] = self.window_start.isoformat()
        return result


@dataclass(frozen=True)
class WindowDecision:
    end_timestamp: pd.Timestamp
    transaction_count: int
    risk_density: float
    baseline_density: float
    density_lift: float
    high_risk_count: int
    expected_high_risk_rate: float
    p_value: float
    volume_lift: float
    required_lift: float
    promo_adjusted: bool
    suspicious: bool
    active_alert: bool

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["end_timestamp"] = self.end_timestamp.isoformat()
        return result


def population_stability_index(
    reference: np.ndarray | list[float], recent: np.ndarray | list[float], bins: int = 10
) -> float:
    """PSI using reference quantile bins; zero for insufficient or constant samples."""

    expected = np.asarray(reference, dtype=float)
    actual = np.asarray(recent, dtype=float)
    if len(expected) < bins or len(actual) < bins or np.ptp(expected) == 0:
        return 0.0
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    expected_hist = np.histogram(expected, bins=edges)[0] / len(expected)
    actual_hist = np.histogram(actual, bins=edges)[0] / len(actual)
    epsilon = 1e-6
    expected_hist = np.clip(expected_hist, epsilon, None)
    actual_hist = np.clip(actual_hist, epsilon, None)
    return float(np.sum((actual_hist - expected_hist) * np.log(actual_hist / expected_hist)))


class RiskDensitySpikeDetector:
    """Detect persistent increases in calibrated risk density, never transaction volume alone."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._history: deque[WindowSnapshot] = deque()
        self._reference_density = 0.0
        self._reference_high_rate = 0.0
        self._reference_volume = 1.0
        self._hour_multipliers = {hour: 1.0 for hour in range(24)}
        self._reference_scores = np.array([], dtype=float)
        self._recent_scores: deque[float] = deque(maxlen=2000)
        self._consecutive_suspicious = 0
        self._inactive_windows = 0
        self._active = False
        self._last_close: pd.Timestamp | None = None
        self._alert_sequence = 0
        self.decisions: list[WindowDecision] = []

    @property
    def active(self) -> bool:
        return self._active

    @property
    def baseline_density(self) -> float:
        return self._reference_density

    def prime(self, windows: list[WindowSnapshot]) -> None:
        """Initialize a robust label-free reference and recent trailing history."""

        supported = [window for window in windows if window.transaction_count >= self.settings.detector_min_support]
        if not supported:
            raise ValueError("Detector priming requires supported windows")
        densities = np.asarray([window.risk_density for window in supported], dtype=float)
        cap = float(np.quantile(densities, 0.80))
        stable = [window for window in supported if window.risk_density <= cap]
        if not stable:
            stable = supported
        self._reference_density = max(float(np.median([window.risk_density for window in stable])), 1e-5)
        self._reference_high_rate = max(
            float(np.median([window.high_risk_count / window.transaction_count for window in stable])),
            1e-5,
        )
        self._reference_volume = max(float(np.median([window.volume_per_hour for window in stable])), 1e-5)
        hour_frame = pd.DataFrame(
            {
                "hour": [window.end_timestamp.hour for window in stable],
                "density": [window.risk_density for window in stable],
            }
        )
        global_density = self._reference_density
        for hour in range(24):
            values = hour_frame.loc[hour_frame["hour"].eq(hour), "density"]
            if len(values) >= 3:
                raw = float(values.median() / global_density)
                self._hour_multipliers[hour] = float(np.clip(0.75 * raw + 0.25, 0.5, 2.0))
        cutoff = supported[-1].end_timestamp - timedelta(
            days=self.settings.detector_baseline_days
        )
        self._history = deque(window for window in stable if window.end_timestamp >= cutoff)
        self._reference_scores = np.concatenate(
            [window.rows["risk_probability"].to_numpy(float) for window in stable]
        )

    def _weighted_baseline(self, timestamp: pd.Timestamp) -> tuple[float, float, float]:
        cutoff = timestamp - timedelta(days=self.settings.detector_baseline_days)
        while self._history and self._history[0].end_timestamp < cutoff:
            self._history.popleft()
        if not self._history:
            return self._reference_density, self._reference_high_rate, self._reference_volume
        half_life = self.settings.detector_ewma_half_life_hours
        weights = np.asarray(
            [
                exp(-log(2) * max((timestamp - item.end_timestamp).total_seconds() / 3600, 0) / half_life)
                for item in self._history
            ]
        )
        density = float(np.average([item.risk_density for item in self._history], weights=weights))
        high_rate = float(
            np.average(
                [item.high_risk_count / max(item.transaction_count, 1) for item in self._history],
                weights=weights,
            )
        )
        volume = float(np.average([item.volume_per_hour for item in self._history], weights=weights))
        hour_adjustment = self._hour_multipliers[timestamp.hour]
        return max(density * hour_adjustment, 1e-5), max(high_rate, 1e-5), max(volume, 1e-5)

    def process(self, window: WindowSnapshot) -> SpikeAlert | None:
        if self._reference_density <= 0:
            raise RuntimeError("Prime the detector before processing evaluation windows")
        baseline_density, expected_high_rate, baseline_volume = self._weighted_baseline(
            window.end_timestamp
        )
        density_lift = window.risk_density / baseline_density
        volume_lift = window.volume_per_hour / baseline_volume
        required_lift = self.settings.detector_lift_threshold
        promo_adjusted = window.promo_share >= self.settings.detector_promo_share_threshold
        if promo_adjusted:
            required_lift += self.settings.detector_promo_lift_margin
        expected_count = window.transaction_count * expected_high_rate
        p_value = float(poisson.sf(window.high_risk_count - 1, expected_count))
        volume_supported = window.transaction_count >= self.settings.detector_min_support
        trigger_supported = (
            volume_supported
            and window.high_risk_count >= self.settings.detector_min_high_risk_count
        )
        suspicious = bool(
            trigger_supported
            and density_lift >= required_lift
            and p_value < self.settings.detector_alpha
        )
        self._recent_scores.extend(window.rows.get("risk_probability", pd.Series(dtype=float)).tolist())

        alert: SpikeAlert | None = None
        if suspicious:
            self._consecutive_suspicious += 1
            self._inactive_windows = 0
            cooldown_over = (
                self._last_close is None
                or window.end_timestamp - self._last_close
                >= timedelta(minutes=self.settings.detector_cooldown_minutes)
            )
            confirmed = (
                self._consecutive_suspicious >= self.settings.detector_confirm_windows
                or density_lift >= self.settings.detector_extreme_lift
            )
            if confirmed and not self._active and cooldown_over:
                self._active = True
                self._alert_sequence += 1
                reason = "persistent calibrated risk-density lift"
                if density_lift >= self.settings.detector_extreme_lift:
                    reason = "extreme calibrated risk-density lift"
                if promo_adjusted:
                    reason += "; promotion context raised, but did not veto, the threshold"
                alert = SpikeAlert(
                    alert_id=f"SPIKE-{window.end_timestamp:%Y%m%d}-{self._alert_sequence:04d}",
                    fire_timestamp=window.end_timestamp,
                    window_start=window.start_timestamp,
                    transaction_count=window.transaction_count,
                    risk_density=window.risk_density,
                    baseline_density=baseline_density,
                    density_lift=density_lift,
                    high_risk_count=window.high_risk_count,
                    expected_high_risk_rate=expected_high_rate,
                    p_value=p_value,
                    volume_lift=volume_lift,
                    promo_share=window.promo_share,
                    required_lift=required_lift,
                    reason=reason,
                    drift_psi=population_stability_index(
                        self._reference_scores, np.asarray(self._recent_scores)
                    ),
                )
        else:
            self._consecutive_suspicious = 0
            if self._active:
                self._inactive_windows += 1
                if self._inactive_windows >= self.settings.detector_inactive_windows_to_close:
                    self._active = False
                    self._last_close = window.end_timestamp
                    self._inactive_windows = 0
            else:
                if volume_supported:
                    self._history.append(window)

        self.decisions.append(
            WindowDecision(
                end_timestamp=window.end_timestamp,
                transaction_count=window.transaction_count,
                risk_density=window.risk_density,
                baseline_density=baseline_density,
                density_lift=density_lift,
                high_risk_count=window.high_risk_count,
                expected_high_risk_rate=expected_high_rate,
                p_value=p_value,
                volume_lift=volume_lift,
                required_lift=required_lift,
                promo_adjusted=promo_adjusted,
                suspicious=suspicious,
                active_alert=self._active,
            )
        )
        return alert
