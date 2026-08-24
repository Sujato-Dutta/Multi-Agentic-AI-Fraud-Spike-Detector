"""Build chronological development incidents and train the production RF reward model."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.ml.artifacts import sha256_file
from backend.app.ml.fraud.predictor import FraudPredictor
from backend.app.ml.reward.reward_model import RewardModel, encode_context_action
from backend.app.services.evaluation_service import ActionEffects, RewardCalculator
from evaluation.dataio import load_split


def build_offline_incidents(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Use train/validation only; each declared spike event becomes one historical incident."""

    config = settings or get_settings()
    effects = ActionEffects.from_yaml(config.action_effects_path)
    calculator = RewardCalculator(effects, config)
    predictor = FraudPredictor.load(config.fraud_primary_model_path)
    incidents: list[dict[str, Any]] = []
    prior_rewards: list[float] = []
    for split_name in ("train", "validation"):
        split = load_split(split_name)
        joined = split.joined
        split_scores = predictor.predict_proba(split.features)
        split_density = float(np.mean(split_scores))
        split_hours = max(
            (split.features["timestamp"].max() - split.features["timestamp"].min()).total_seconds()
            / 3600,
            1,
        )
        hourly_volume = len(split.features) / split_hours
        for event in split.spike_events.to_dict("records"):
            mask = joined["timestamp"].between(
                event["start_timestamp"], event["end_timestamp"], inclusive="both"
            )
            rows = joined.loc[mask].copy()
            if rows.empty:
                continue
            feature_rows = split.features.loc[
                split.features["transaction_id"].isin(rows["transaction_id"])
            ]
            probabilities = predictor.predict_proba(feature_rows)
            duration_hours = max(
                (event["end_timestamp"] - event["start_timestamp"]).total_seconds()
                / 3600,
                1,
            )
            context = {
                "fraud_probability_mean": float(np.mean(probabilities)),
                "fraud_probability_max": float(np.max(probabilities)),
                "density_lift": float(np.mean(probabilities) / max(split_density, 1e-9)),
                "volume_lift": float(len(rows) / max(hourly_volume * duration_hours, 1)),
                "segment_support": float(len(rows)),
                "segment_breadth": 1.0,
                "amount_mean_inr": float(rows["amount_inr"].mean()),
                "amount_max_inr": float(rows["amount_inr"].max()),
                "agent_confidence": 0.0,
                "grounding_score": 0.0,
                "historical_segment_fraud_rate": float(rows["is_fraud"].mean()),
                "promotion_context": float(rows["known_promo_event"].mean()),
                "similar_incident_mean_reward": (
                    float(np.mean(prior_rewards)) if prior_rewards else 0.0
                ),
                "similar_incident_rejection_rate": 0.0,
            }
            rewards = {
                item.action: item.model_dump(mode="json")
                for item in calculator.counterfactuals(rows.to_dict("records"))
            }
            prior_rewards.append(max(item["total_reward_inr"] for item in rewards.values()))
            incidents.append(
                {
                    "incident_id": str(event["event_id"]),
                    "split": split_name,
                    "timestamp": event["start_timestamp"].isoformat(),
                    "scenario_signature": str(event["scenario_family"]),
                    "context": context,
                    "rewards": rewards,
                    "fraud_loss_total_inr": float(
                        rows.loc[rows["is_fraud"].eq(1), "fraud_loss_if_missed_inr"].sum()
                    ),
                    "legitimate_value_inr": float(
                        rows.loc[rows["is_fraud"].eq(0), "amount_inr"].sum()
                    ),
                    "assumptions_version": effects.version,
                }
            )
    return sorted(incidents, key=lambda item: item["timestamp"])


def flattened_examples(
    incidents: list[dict[str, Any]],
) -> tuple[list[dict[str, float]], list[str], list[float]]:
    contexts: list[dict[str, float]] = []
    actions: list[str] = []
    rewards: list[float] = []
    for incident in incidents:
        for action, result in incident["rewards"].items():
            contexts.append(incident["context"])
            actions.append(action)
            rewards.append(float(result["total_reward_inr"]))
    return contexts, actions, rewards


def train_reward_model(settings: Settings | None = None) -> dict[str, Any]:
    config = settings or get_settings()
    incidents = build_offline_incidents(config)
    contexts, actions, rewards = flattened_examples(incidents)
    assumptions_version = str(incidents[0]["assumptions_version"])
    model = RewardModel(
        estimators=config.reward_model_estimators, random_seed=config.random_seed
    ).fit(contexts, actions, rewards, assumptions_version=assumptions_version)
    predictions = model.predict(contexts, actions)
    matrix = np.vstack(
        [encode_context_action(context, action) for context, action in zip(contexts, actions)]
    )
    benchmark = GradientBoostingRegressor(random_state=config.random_seed).fit(
        matrix, np.asarray(rewards)
    )
    metrics = {
        "training_incidents": len(incidents),
        "training_examples": len(rewards),
        "rf_training_mae_inr": float(mean_absolute_error(rewards, predictions)),
        "gradient_boosting_training_mae_inr": float(
            mean_absolute_error(rewards, benchmark.predict(matrix))
        ),
        "assumptions_version": assumptions_version,
        "heldout_test_accessed": False,
    }
    artifact = model.save(config.reward_model_path)
    metrics["artifact_checksum"] = sha256_file(artifact)
    metadata = artifact.with_suffix(".json")
    metadata.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    mlflow.set_tracking_uri(config.mlflow_tracking_uri)
    mlflow.set_experiment("response-reward-model")
    with mlflow.start_run(run_name="random-forest-counterfactual-reward"):
        mlflow.log_params(
            {
                "family": "RandomForestRegressor",
                "estimators": config.reward_model_estimators,
                "assumptions_version": assumptions_version,
                "random_seed": config.random_seed,
            }
        )
        mlflow.log_metrics(
            {
                key: value
                for key, value in metrics.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        )
        mlflow.log_artifact(str(artifact), artifact_path="reward_model")
        mlflow.log_artifact(str(metadata), artifact_path="reward_model")
    return {"artifact": str(artifact), "metrics": metrics, "incidents": incidents}


if __name__ == "__main__":
    print(json.dumps(train_reward_model(), indent=2, default=str))
