"""Train a LinUCB candidate on chronological development incidents only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mlflow
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.ml.artifacts import sha256_file
from backend.app.ml.policy.contextual_bandit import (
    LinUCBPolicy,
    offline_thompson_scores,
)
from training.train_reward_model import build_offline_incidents, flattened_examples


def chronological_policy_split(
    incidents: list[dict[str, Any]], holdback_fraction: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validation = [item for item in incidents if item["split"] == "validation"]
    holdback_count = max(1, int(np.ceil(len(validation) * holdback_fraction)))
    holdback_ids = {item["incident_id"] for item in validation[-holdback_count:]}
    fit = [item for item in incidents if item["incident_id"] not in holdback_ids]
    holdback = [item for item in incidents if item["incident_id"] in holdback_ids]
    if not fit or not holdback:
        raise ValueError("Chronological policy split requires fit and held-back incidents")
    return fit, holdback


def train_bandit(settings: Settings | None = None) -> dict[str, Any]:
    config = settings or get_settings()
    incidents = build_offline_incidents(config)
    fit_incidents, holdback = chronological_policy_split(
        incidents, config.policy_holdback_fraction
    )
    contexts, actions, rewards = flattened_examples(fit_incidents)
    assumptions_version = str(incidents[0]["assumptions_version"])
    candidate = LinUCBPolicy(alpha=config.linucb_alpha).fit(
        contexts, actions, rewards, assumptions_version=assumptions_version
    )
    artifact = candidate.save(config.candidate_policy_path)
    greedy_rewards = []
    thompson_rewards = []
    for index, incident in enumerate(fit_incidents):
        context = incident["context"]
        greedy = candidate.greedy_action(context)
        greedy_rewards.append(float(incident["rewards"][greedy]["total_reward_inr"]))
        thompson_scores = offline_thompson_scores(
            candidate, context, random_seed=config.random_seed + index
        )
        thompson = max(thompson_scores, key=thompson_scores.get)
        thompson_rewards.append(
            float(incident["rewards"][thompson]["total_reward_inr"])
        )
    metrics = {
        "fit_incidents": len(fit_incidents),
        "holdback_incidents": len(holdback),
        "fit_end_timestamp": fit_incidents[-1]["timestamp"],
        "holdback_start_timestamp": holdback[0]["timestamp"],
        "greedy_fit_expected_reward_inr": float(np.mean(greedy_rewards)),
        "thompson_benchmark_expected_reward_inr": float(np.mean(thompson_rewards)),
        "assumptions_version": assumptions_version,
        "heldout_test_accessed": False,
        "live_exploration": False,
    }
    manifest = {
        "artifact": str(artifact),
        "artifact_checksum": sha256_file(artifact),
        "assumptions_version": assumptions_version,
        "status": "candidate",
        "production_eligible": False,
        "promotion_requires_admin": True,
        "metrics": metrics,
    }
    manifest_path = artifact.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    mlflow.set_tracking_uri(config.mlflow_tracking_uri)
    mlflow.set_experiment("response-contextual-bandit")
    with mlflow.start_run(run_name="linucb-offline-candidate"):
        mlflow.log_params(
            {
                "family": "LinUCB",
                "alpha": config.linucb_alpha,
                "assumptions_version": assumptions_version,
                "live_exploration": False,
            }
        )
        mlflow.log_metrics(
            {
                key: value
                for key, value in metrics.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        )
        mlflow.log_artifact(str(artifact), artifact_path="response_policy")
        mlflow.log_artifact(str(manifest_path), artifact_path="response_policy")
    return {**manifest, "holdback_incidents": holdback}


if __name__ == "__main__":
    print(json.dumps(train_bandit(), indent=2, default=str))
