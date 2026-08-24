"""Offline LinUCB response policy; serving is greedy and never explores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from backend.app.ml.artifacts import dump_verified, load_verified
from backend.app.ml.reward.reward_model import CONTEXT_FEATURES, encode_context
from backend.app.services.evaluation_service import RESPONSE_ACTIONS

ACTIONS = RESPONSE_ACTIONS
POLICY_ARTIFACT_VERSION = 1


class LinUCBPolicy:
    def __init__(self, *, alpha: float = 0.25, ridge: float = 1.0) -> None:
        if alpha < 0 or ridge <= 0:
            raise ValueError("LinUCB alpha must be non-negative and ridge positive")
        self.alpha = float(alpha)
        self.ridge = float(ridge)
        dimension = len(CONTEXT_FEATURES)
        self.a = {
            action: np.eye(dimension, dtype=float) * ridge for action in ACTIONS
        }
        self.b = {action: np.zeros(dimension, dtype=float) for action in ACTIONS}
        self.assumptions_version: str | None = None
        self.fitted = False

    def update(self, context: Mapping[str, Any], action: str, reward: float) -> None:
        if action not in ACTIONS or not np.isfinite(reward):
            raise ValueError("LinUCB update requires a known action and finite reward")
        vector = encode_context(context)
        self.a[action] += np.outer(vector, vector)
        self.b[action] += float(reward) * vector
        self.fitted = True

    def fit(
        self,
        contexts: Sequence[Mapping[str, Any]],
        actions: Sequence[str],
        rewards: Sequence[float],
        *,
        assumptions_version: str,
    ) -> LinUCBPolicy:
        if not contexts or len(contexts) != len(actions) or len(actions) != len(rewards):
            raise ValueError("Bandit training arrays must be non-empty and aligned")
        for context, action, reward in zip(contexts, actions, rewards):
            self.update(context, action, float(reward))
        self.assumptions_version = assumptions_version
        return self

    def scores(
        self, context: Mapping[str, Any], *, offline_exploration: bool = False
    ) -> dict[str, float]:
        vector = encode_context(context)
        output: dict[str, float] = {}
        for action in ACTIONS:
            inverse = np.linalg.inv(self.a[action])
            theta = inverse @ self.b[action]
            expected = float(theta @ vector)
            uncertainty = float(np.sqrt(max(vector @ inverse @ vector, 0.0)))
            output[action] = expected + (self.alpha * uncertainty if offline_exploration else 0)
        return output

    def rank(self, context: Mapping[str, Any]) -> list[dict[str, float | str]]:
        """Production path: deterministic greedy ranking without exploration."""

        return [
            {"action": action, "expected_reward_inr": score}
            for action, score in sorted(
                self.scores(context).items(), key=lambda item: item[1], reverse=True
            )
        ]

    def greedy_action(self, context: Mapping[str, Any]) -> str:
        return str(self.rank(context)[0]["action"])

    def save(self, path: Path | str) -> Path:
        if not self.fitted or not self.assumptions_version:
            raise ValueError("Cannot save an unfitted response policy")
        target = Path(path)
        dump_verified(
            {
                "artifact_version": POLICY_ARTIFACT_VERSION,
                "context_features": CONTEXT_FEATURES,
                "actions": ACTIONS,
                "alpha": self.alpha,
                "ridge": self.ridge,
                "assumptions_version": self.assumptions_version,
                "a": self.a,
                "b": self.b,
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
    ) -> LinUCBPolicy:
        artifact: dict[str, Any] = load_verified(path, expected_checksum)
        if (
            artifact.get("artifact_version") != POLICY_ARTIFACT_VERSION
            or tuple(artifact.get("context_features", ())) != CONTEXT_FEATURES
            or tuple(artifact.get("actions", ())) != ACTIONS
        ):
            raise ValueError("Response-policy artifact schema is incompatible")
        if assumptions_version and artifact.get("assumptions_version") != assumptions_version:
            raise ValueError("Response policy uses a different action-effects version")
        policy = cls(alpha=float(artifact["alpha"]), ridge=float(artifact["ridge"]))
        policy.a = artifact["a"]
        policy.b = artifact["b"]
        policy.assumptions_version = str(artifact["assumptions_version"])
        policy.fitted = True
        return policy


def offline_thompson_scores(
    policy: LinUCBPolicy, context: Mapping[str, Any], *, random_seed: int
) -> dict[str, float]:
    """Logged benchmark only; intentionally separate from the serving interface."""

    vector = encode_context(context)
    rng = np.random.default_rng(random_seed)
    output = {}
    for action in ACTIONS:
        inverse = np.linalg.inv(policy.a[action])
        mean = inverse @ policy.b[action]
        sampled = rng.multivariate_normal(mean, inverse)
        output[action] = float(sampled @ vector)
    return output
