"""Central construction of compact, collision-safe Redis keys."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import quote

import orjson

KeyNamespace = Literal[
    "txn",
    "feature",
    "prediction",
    "detector",
    "agent_result",
    "rate_limit",
    "session",
    "demo",
]

_PREFIXES: dict[KeyNamespace, str] = {
    "txn": "txn:",
    "feature": "feature:",
    "prediction": "prediction:",
    "detector": "detector:",
    "agent_result": "agent-result:",
    "rate_limit": "rate-limit:",
    "session": "session:",
    "demo": "demo:",
}
ALL_PREFIXES = frozenset(_PREFIXES.values())


def namespace_prefix(namespace: KeyNamespace) -> str:
    return _PREFIXES[namespace]


def demo_stream_key(scenario: str) -> str:
    return _key("demo", "stream", scenario)


def transaction_key(transaction_id: str) -> str:
    return _key("txn", transaction_id)


def feature_key(transaction_id: str) -> str:
    return _key("feature", transaction_id)


def prediction_key(transaction_id: str, model_version: str = "active") -> str:
    return _key("prediction", model_version, transaction_id)


def detector_state_key(detector_id: str = "default") -> str:
    return _key("detector", detector_id)


def agent_result_key(tier: str, prompt_hash: str, evidence_hash: str) -> str:
    return _key("agent_result", tier, prompt_hash, evidence_hash)


def rate_limit_key(scope: str, identity: str, bucket: int) -> str:
    if bucket < 0:
        raise ValueError("Rate-limit bucket must be non-negative")
    return _key("rate_limit", scope, identity, str(bucket))


def session_key(session_id: str) -> str:
    return _key("session", session_id)


def hash_evidence(value: object) -> str:
    """Return a stable SHA-256 digest for canonical JSON-compatible evidence."""

    payload = orjson.dumps(value, option=orjson.OPT_SORT_KEYS, default=_json_default)
    return hashlib.sha256(payload).hexdigest()


def _key(namespace: KeyNamespace, *parts: str) -> str:
    return _PREFIXES[namespace] + ":".join(_component(part) for part in parts)


def _component(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("Cache key components must be non-empty")
    return quote(normalized, safe="-_.~")


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Evidence value {type(value).__name__} is not JSON serializable")
