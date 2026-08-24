"""Deterministic, label-free discovery of cohorts driving excess risk."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import log1p

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact


@dataclass(frozen=True)
class SegmentFinding:
    conditions: tuple[str, ...]
    support: int
    baseline_support: int
    risk_density: float
    baseline_risk_density: float
    density_lift: float
    prevalence_lift: float
    excess_risk_contribution: float
    p_value: float
    score: float
    condition_contributions: tuple[dict[str, float | int | str], ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["conditions"] = list(self.conditions)
        result["condition_contributions"] = list(self.condition_contributions)
        return result


_BINARY_AND_CATEGORICAL = (
    "is_new_device",
    "is_proxy_ip",
    "billing_shipping_mismatch",
    "payment_method",
    "channel",
    "merchant_category",
    "ip_cluster_group",
)
_NUMERIC_BINS = {
    "amount_inr": (0, 500, 2_000, 5_000, 10_000, np.inf),
    "txn_velocity_1h": (-np.inf, 0, 1, 3, 6, np.inf),
    "geo_distance_km": (-np.inf, 25, 100, 500, 1_000, np.inf),
    "customer_age_days": (-np.inf, 30, 90, 365, 730, np.inf),
    "account_changes_24h": (-np.inf, 0, 1, 2, np.inf),
    "failed_attempts_24h": (-np.inf, 0, 1, 3, np.inf),
}


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    if "risk_probability" not in frame:
        raise ValueError("Segment discovery requires calibrated risk_probability")
    result = frame.copy()
    if "ip_cluster_group" not in result and "ip_cluster_id" in result:
        result["ip_cluster_group"] = result["ip_cluster_id"].astype("string").str.extract(
            r"^(IP\d{2})", expand=False
        )
    for column, bins in _NUMERIC_BINS.items():
        if column in result:
            result[f"{column}_band"] = pd.cut(result[column], bins=bins, include_lowest=True).astype(
                "string"
            )
    return result


def _atomic_conditions(current: pd.DataFrame, min_support: int) -> list[tuple[str, object, str]]:
    columns = [column for column in _BINARY_AND_CATEGORICAL if column in current]
    columns.extend(f"{column}_band" for column in _NUMERIC_BINS if f"{column}_band" in current)
    conditions: list[tuple[str, object, str]] = []
    for column in columns:
        counts = current[column].value_counts(dropna=True)
        for value, count in counts.items():
            if count >= min_support:
                conditions.append((column, value, f"{column}={value}"))
    return conditions


def _segment_stat(
    current: pd.DataFrame,
    baseline: pd.DataFrame,
    path: tuple[tuple[str, object, str], ...],
    min_support: int,
    alpha: float,
) -> SegmentFinding | None:
    current_mask = pd.Series(True, index=current.index)
    baseline_mask = pd.Series(True, index=baseline.index)
    for column, value, _ in path:
        current_mask &= current[column].eq(value)
        baseline_mask &= baseline[column].eq(value)
    selected = current.loc[current_mask]
    reference = baseline.loc[baseline_mask]
    if len(selected) < min_support or len(reference) < min_support:
        return None

    global_baseline = max(float(baseline["risk_probability"].mean()), 1e-5)
    density = float(selected["risk_probability"].mean())
    reference_density = max(float(reference["risk_probability"].mean()), global_baseline * 0.25, 1e-5)
    lift = density / reference_density
    prevalence_lift = (len(selected) / len(current)) / (len(reference) / len(baseline))
    threshold = max(float(baseline["risk_probability"].quantile(0.99)), 0.20)
    current_high = int(selected["risk_probability"].ge(threshold).sum())
    baseline_high = int(reference["risk_probability"].ge(threshold).sum())
    table = [
        [current_high, len(selected) - current_high],
        [baseline_high, len(reference) - baseline_high],
    ]
    p_value = float(fisher_exact(table, alternative="greater").pvalue)
    current_total_excess = max(
        float(current["risk_probability"].sum()) - len(current) * global_baseline, 1e-9
    )
    segment_excess = max(float(selected["risk_probability"].sum()) - len(selected) * global_baseline, 0.0)
    contribution = segment_excess / current_total_excess
    complexity_penalty = 2 ** (len(path) - 1)
    score = (
        lift
        * prevalence_lift
        * log1p(len(selected))
        * (1 + min(contribution, 2.0))
        / complexity_penalty
    )
    if lift <= 1 or prevalence_lift < 1.10 or p_value > alpha or contribution <= 0:
        return None

    marginal: list[dict[str, float | int | str]] = []
    parent_current = current
    parent_baseline = baseline
    parent_lift = float(current["risk_probability"].mean()) / global_baseline
    for column, value, label in path:
        child_current = parent_current.loc[parent_current[column].eq(value)]
        child_baseline = parent_baseline.loc[parent_baseline[column].eq(value)]
        child_density = float(child_current["risk_probability"].mean())
        child_baseline_density = max(
            float(child_baseline["risk_probability"].mean()), global_baseline * 0.25, 1e-5
        )
        child_lift = child_density / child_baseline_density
        marginal.append(
            {
                "condition": label,
                "parent_support": len(parent_current),
                "support": len(child_current),
                "support_retention": float(len(child_current) / max(len(parent_current), 1)),
                "parent_density_lift": float(parent_lift),
                "density_lift": float(child_lift),
                "marginal_density_lift": float(child_lift - parent_lift),
            }
        )
        parent_current = child_current
        parent_baseline = child_baseline
        parent_lift = child_lift
    return SegmentFinding(
        conditions=tuple(condition[2] for condition in path),
        support=len(selected),
        baseline_support=len(reference),
        risk_density=density,
        baseline_risk_density=reference_density,
        density_lift=lift,
        prevalence_lift=prevalence_lift,
        excess_risk_contribution=contribution,
        p_value=p_value,
        score=score,
        condition_contributions=tuple(marginal),
    )


def discover_segments(
    current_window: pd.DataFrame,
    baseline_transactions: pd.DataFrame,
    *,
    min_support: int = 10,
    max_depth: int = 3,
    alpha: float = 0.05,
    top_k: int = 5,
) -> list[SegmentFinding]:
    """Greedily discover up to depth-3 intersections ranked by risk lift and contribution."""

    if min_support < 2 or not 1 <= max_depth <= 3 or top_k < 1:
        raise ValueError("Invalid segmentation limits")
    current = _prepare(current_window)
    baseline = _prepare(baseline_transactions)
    conditions = _atomic_conditions(current, min_support)
    frontier: list[tuple[tuple[str, object, str], ...]] = [(condition,) for condition in conditions]
    findings: list[SegmentFinding] = []

    for depth in range(1, max_depth + 1):
        depth_findings: list[tuple[SegmentFinding, tuple[tuple[str, object, str], ...]]] = []
        for path in frontier:
            finding = _segment_stat(current, baseline, path, min_support, alpha)
            if finding is not None:
                findings.append(finding)
                depth_findings.append((finding, path))
        if depth == max_depth or not depth_findings:
            break
        best_paths = [path for _, path in sorted(depth_findings, key=lambda item: item[0].score, reverse=True)[:8]]
        next_frontier = []
        for path in best_paths:
            used = {condition[0] for condition in path}
            for condition in conditions:
                if condition[0] not in used:
                    next_frontier.append(path + (condition,))
        frontier = next_frontier

    unique: dict[tuple[str, ...], SegmentFinding] = {}
    for finding in findings:
        key = tuple(sorted(finding.conditions))
        if key not in unique or finding.score > unique[key].score:
            unique[key] = finding
    return sorted(unique.values(), key=lambda finding: finding.score, reverse=True)[:top_k]
