"""Random-Forest expected-reward model with one canonical context/action schema."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestRegressor

from backend.app.ml.artifacts import dump_verified, load_verified
from backend.app.services.evaluation_service import RESPONSE_ACTIONS

CONTEXT_FEATURES: tuple[str, ...] = (
    "fraud_probability_mean",
    "fraud_probability_max",
    "density_lift",
    "volume_lift",
    "segment_support",
    "segment_breadth",
    "amount_mean_inr",
    "amount_max_inr",
    "agent_confidence",
    "grounding_score",
    "historical_segment_fraud_rate",
    "promotion_context",
    "similar_incident_mean_reward",
    "similar_incident_rejection_rate",
)
FEATURE_SCHEMA_VERSION = "reward-context-v1"


class RewardModel:
    """Production reward family: RandomForestRegressor only."""

    def __init__(self, *, estimators: int = 200, random_seed: int = 20260822) -> None:
        self.estimators = estimators
        self.random_seed = random_seed
        self.model = RandomForestRegressor(
            n_estimators=estimators,
            min_samples_leaf=2,
            random_state=random_seed,
            n_jobs=-1,
        )
        self.assumptions_version: str | None = None
        self.fitted = False

    @property
    def dimension(self) -> int:
        return len(CONTEXT_FEATURES) + len(RESPONSE_ACTIONS)

    def fit(
        self,
        contexts: Sequence[Mapping[str, Any]],
        actions: Sequence[str],
        rewards: Sequence[float],
        *,
        assumptions_version: str,
    ) -> RewardModel:
        if not contexts or len(contexts) != len(actions) or len(actions) != len(rewards):
            raise ValueError("Reward training arrays must be non-empty and aligned")
        matrix = np.vstack([encode_context_action(c, a) for c, a in zip(contexts, actions)])
        target = np.asarray(rewards, dtype=float)
        if not np.isfinite(target).all():
            raise ValueError("Reward targets must be finite")
        self.model.fit(matrix, target)
        self.assumptions_version = assumptions_version
        self.fitted = True
        return self

    def predict(
        self, contexts: Sequence[Mapping[str, Any]], actions: Sequence[str]
    ) -> np.ndarray:
        if not self.fitted or len(contexts) != len(actions):
            raise ValueError("Reward model must be fitted and prediction arrays aligned")
        matrix = np.vstack([encode_context_action(c, a) for c, a in zip(contexts, actions)])
        return self.model.predict(matrix)

    def rank(self, context: Mapping[str, Any]) -> list[dict[str, float | str]]:
        scores = self.predict([context] * len(RESPONSE_ACTIONS), list(RESPONSE_ACTIONS))
        return sorted(
            (
                {"action": action, "expected_reward_inr": float(score)}
                for action, score in zip(RESPONSE_ACTIONS, scores)
            ),
            key=lambda item: float(item["expected_reward_inr"]),
            reverse=True,
        )

    def save(self, path: Path | str) -> Path:
        if not self.fitted or not self.assumptions_version:
            raise ValueError("Cannot save an unfitted reward model")
        target = Path(path)
        dump_verified(
            {
                "artifact_version": 1,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "context_features": CONTEXT_FEATURES,
                "actions": RESPONSE_ACTIONS,
                "assumptions_version": self.assumptions_version,
                "estimators": self.estimators,
                "random_seed": self.random_seed,
                "model": self.model,
            },
            target,
        )
        return target

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        assumptions_version: str | None = None,
        expected_checksum: str | None = None,
    ) -> RewardModel:
        artifact = load_verified(path, expected_checksum)
        if (
            artifact.get("feature_schema_version") != FEATURE_SCHEMA_VERSION
            or tuple(artifact.get("context_features", ())) != CONTEXT_FEATURES
            or tuple(artifact.get("actions", ())) != RESPONSE_ACTIONS
        ):
            raise ValueError("Reward artifact schema is incompatible")
        if assumptions_version and artifact.get("assumptions_version") != assumptions_version:
            raise ValueError("Reward artifact uses a different action-effects version")
        instance = cls(
            estimators=int(artifact["estimators"]),
            random_seed=int(artifact["random_seed"]),
        )
        instance.model = artifact["model"]
        instance.assumptions_version = str(artifact["assumptions_version"])
        instance.fitted = True
        return instance


def encode_context(context: Mapping[str, Any]) -> np.ndarray:
    values = []
    for name in CONTEXT_FEATURES:
        value = context.get(name)
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"Reward context requires finite numeric feature {name}")
        values.append(float(value))
    return np.asarray(values, dtype=float)


def encode_context_action(context: Mapping[str, Any], action: str) -> np.ndarray:
    if action not in RESPONSE_ACTIONS:
        raise ValueError(f"Unknown response action: {action}")
    one_hot = np.zeros(len(RESPONSE_ACTIONS), dtype=float)
    one_hot[RESPONSE_ACTIONS.index(action)] = 1.0
    return np.concatenate((encode_context(context), one_hot))
