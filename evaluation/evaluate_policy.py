"""Measured shadow comparison on a frozen chronological validation holdback."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.ml.artifacts import sha256_file
from backend.app.ml.policy.contextual_bandit import LinUCBPolicy
from backend.app.ml.policy.shadow_policy import PolicyMetrics, PromotionGate
from backend.app.safety.policy_engine import PolicyContext, PolicyEngine
from backend.app.services.policy_service import (
    BUILTIN_CONSERVATIVE_IDENTITY,
    build_promotion_evidence,
)
from training.train_bandit import chronological_policy_split
from training.train_reward_model import build_offline_incidents


def evaluate_actions(
    incidents: list[dict[str, Any]], chooser: Callable[[dict[str, float]], str]
) -> dict[str, Any]:
    safety = PolicyEngine.default()
    selected = []
    reward_values = []
    fraud_prevented = 0.0
    fraud_total = 0.0
    fp_cost = 0.0
    violations = 0
    for incident in incidents:
        action = chooser(incident["context"])
        result = incident["rewards"][action]
        selected.append(action)
        reward_values.append(float(result["total_reward_inr"]))
        fraud_prevented += float(result["fraud_prevented_inr"])
        fraud_total += float(incident["fraud_loss_total_inr"])
        fp_cost += float(result["false_positive_cost_inr"])
        decision = safety.evaluate(
            action,
            PolicyContext(
                affected_legitimate_value_inr=float(incident["legitimate_value_inr"]),
                fraud_exposure_inr=float(incident["fraud_loss_total_inr"]),
                segment_breadth=float(incident["context"]["segment_breadth"]),
                grounding_score=1,
                confidence_score=1,
                novelty_score=1,
                actor_role="lead_analyst",
            ),
        )
        violations += int(decision.decision == "deny")
    metrics = PolicyMetrics(
        expected_reward_inr=float(np.mean(reward_values)),
        precision=(fraud_prevented / max(fraud_prevented + fp_cost, 1e-9)),
        recall=(fraud_prevented / max(fraud_total, 1e-9)),
        false_positive_cost_inr=fp_cost,
        fraud_value_captured_inr=fraud_prevented,
        escalation_rate=sum(
            action in {"human_escalation", "manual_review"} for action in selected
        )
        / len(selected),
        safety_violations=violations,
        evaluated_incidents=len(selected),
    )
    return {"metrics": metrics.model_dump(mode="json"), "actions": selected}


def evaluate_policy(settings: Settings | None = None) -> dict[str, Any]:
    config = settings or get_settings()
    incidents = build_offline_incidents(config)
    _, holdback = chronological_policy_split(incidents, config.policy_holdback_fraction)
    assumptions_version = str(incidents[0]["assumptions_version"])
    candidate = LinUCBPolicy.load(
        config.candidate_policy_path, assumptions_version=assumptions_version
    )
    candidate_checksum = sha256_file(config.candidate_policy_path)
    if config.production_policy_path.exists():
        production_policy = LinUCBPolicy.load(
            config.production_policy_path, assumptions_version=assumptions_version
        )
        production = evaluate_actions(holdback, production_policy.greedy_action)
        production_identity = sha256_file(config.production_policy_path)
    else:
        production = evaluate_actions(holdback, lambda _: "human_escalation")
        production_identity = BUILTIN_CONSERVATIVE_IDENTITY
    always_escalate = evaluate_actions(holdback, lambda _: "human_escalation")
    step_up = evaluate_actions(holdback, lambda _: "step_up_verification")
    candidate_result = evaluate_actions(holdback, candidate.greedy_action)
    gate = PromotionGate(
        reward_margin_inr=config.policy_reward_margin_inr,
        recall_tolerance=config.policy_recall_tolerance,
        fp_cost_tolerance=config.policy_fp_cost_tolerance,
    ).evaluate(
        PolicyMetrics.model_validate(candidate_result["metrics"]),
        PolicyMetrics.model_validate(production["metrics"]),
    )
    candidate_reward = candidate_result["metrics"]["expected_reward_inr"]
    holdback_manifest = {
        "incidents": [item["incident_id"] for item in holdback],
        "start_timestamp": holdback[0]["timestamp"],
        "selection": "chronological_validation_tail_frozen_before_scoring",
    }
    evidence = build_promotion_evidence(
        candidate_metrics=candidate_result["metrics"],
        production_metrics=production["metrics"],
        holdback=holdback_manifest,
        assumptions_version=assumptions_version,
        candidate_checksum=candidate_checksum,
        production_identity=production_identity,
    )
    report = {
        "report_type": "phase6_shadow_policy_validation",
        "heldout_test_accessed": False,
        "assumptions_version": assumptions_version,
        "assumptions_path": str(config.action_effects_path),
        "holdback": holdback_manifest,
        "production_identity": production_identity,
        "candidate_artifact_checksum": candidate_checksum,
        "production": production,
        "candidate": candidate_result,
        "always_escalate": always_escalate,
        "always_step_up": step_up,
        "candidate_beats_always_escalate": candidate_reward
        > always_escalate["metrics"]["expected_reward_inr"],
        "candidate_beats_always_step_up": candidate_reward
        > step_up["metrics"]["expected_reward_inr"],
        "promotion_gate": gate.model_dump(mode="json"),
        "promotion_evidence": evidence,
        "automatic_promotion": False,
    }
    output_dir = config.report_dir / "metrics"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase6_shadow_policy.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path = output_dir / "phase6_shadow_policy.md"
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict[str, Any]) -> str:
    rows = []
    for name in ("production", "candidate", "always_escalate", "always_step_up"):
        metrics = report[name]["metrics"]
        rows.append(
            f"| {name} | ₹{metrics['expected_reward_inr']:,.2f} | "
            f"{metrics['recall']:.3f} | ₹{metrics['false_positive_cost_inr']:,.2f} | "
            f"{metrics['safety_violations']} |"
        )
    return "\n".join(
        [
            "# Phase 6 Shadow Policy Validation",
            "",
            "Development validation holdback only; held-out test labels were not accessed.",
            "Action effects are explicit assumptions, not observed treatment effects.",
            "",
            "| Policy | Expected reward | Recall | FP cost | Safety violations |",
            "|---|---:|---:|---:|---:|",
            *rows,
            "",
            (
                f"Promotion gate passed: **{report['promotion_gate']['passed']}**. "
                "Promotion remains an explicit admin action; no automatic path exists."
            ),
        ]
    )


if __name__ == "__main__":
    print(json.dumps(evaluate_policy(), indent=2))
