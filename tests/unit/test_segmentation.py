from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.ml.spike_detection.segmentation import discover_segments


def _fixtures() -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(20260822)
    baseline_proxy = np.r_[np.ones(30, dtype=int), np.zeros(970, dtype=int)]
    rng.shuffle(baseline_proxy)
    baseline = pd.DataFrame(
        {
            "is_proxy_ip": baseline_proxy,
            "payment_method": rng.choice(["upi", "card"], size=1000, p=[0.6, 0.4]),
            "risk_probability": np.where(baseline_proxy == 1, 0.10, 0.02),
        }
    )
    current_proxy = np.r_[np.ones(70, dtype=int), np.zeros(30, dtype=int)]
    current = pd.DataFrame(
        {
            "is_proxy_ip": current_proxy,
            "payment_method": np.where(current_proxy == 1, "upi", "card"),
            "risk_probability": np.where(current_proxy == 1, 0.80, 0.02),
        }
    )
    return current, baseline


def test_discovers_planted_proxy_segment_without_ground_truth_labels() -> None:
    current, baseline = _fixtures()
    findings = discover_segments(current, baseline, min_support=10, max_depth=1, top_k=3)

    assert findings
    assert findings[0].conditions == ("is_proxy_ip=1",)
    assert findings[0].support == 70
    assert findings[0].prevalence_lift > 20
    assert findings[0].density_lift > 5
    assert "is_fraud" not in current and "is_fraud" not in baseline


def test_segment_search_respects_depth_support_and_top_k_limits() -> None:
    current, baseline = _fixtures()
    findings = discover_segments(current, baseline, min_support=20, max_depth=3, top_k=4)

    assert len(findings) <= 4
    assert all(finding.support >= 20 and finding.baseline_support >= 20 for finding in findings)
    assert all(1 <= len(finding.conditions) <= 3 for finding in findings)
    assert all(finding.p_value <= 0.05 for finding in findings)


def test_multicondition_evidence_reports_marginal_not_repeated_intersection_values() -> None:
    current, baseline = _fixtures()
    findings = discover_segments(current, baseline, min_support=10, max_depth=2, top_k=10)
    multi = next(finding for finding in findings if len(finding.conditions) == 2)
    contributions = multi.condition_contributions

    assert len(contributions) == 2
    assert contributions[0]["parent_support"] == len(current)
    assert contributions[1]["parent_support"] == contributions[0]["support"]
    marginal_lifts = [float(item["marginal_density_lift"]) for item in contributions]
    assert "marginal_density_lift" in contributions[0]
    assert "excess_risk_share" not in contributions[0]
    assert not np.isclose(marginal_lifts[0], marginal_lifts[1])
