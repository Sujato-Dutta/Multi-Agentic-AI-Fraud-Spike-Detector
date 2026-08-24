"""Scripted failure drills for the documented degradation matrix.

Each drill forces one failure, records the observed behaviour, and compares it with the
expected visible outcome. Every drill runs in-process, so the checklist can be produced
without Docker, a broker, or model credentials.

Usage:
    python scripts/failure_drills.py            # run all drills, write the checklist
    python scripts/failure_drills.py --list     # show drill names
    python scripts/failure_drills.py --only redis_down
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from backend.app.agents.state import AlertExplanation
from backend.app.cache.cache_service import CacheService
from backend.app.config import Settings
from backend.app.core.runtime import AppError, DegradationState
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier
from backend.app.ml.fraud.predictor import ResilientFraudScorer
from backend.app.ml.policy.shadow_policy import (
    PolicyMetrics,
    PromotionGate,
    ShadowPolicy,
)
from backend.app.safety.evidence_grounding import ground_claims
from backend.app.safety.policy_engine import PolicyContext, PolicyEngine
from backend.app.streaming.producer import EventProducer
from evaluation.dataio import load_features


@dataclass
class DrillResult:
    name: str
    scenario: str
    expected: str
    observed: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario": self.scenario,
            "expected": self.expected,
            "observed": self.observed,
            "passed": self.passed,
            "evidence": self.evidence,
        }


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        jwt_secret_key="failure-drill-secret-at-least-32-bytes-long",
        service_token="failure-drill-service-token",
    )


class BrokenRedis:
    """Every operation fails, exactly as an unreachable Redis would."""

    async def ping(self) -> bool:
        raise OSError("redis unreachable")

    async def get(self, key: str) -> bytes | None:
        raise OSError("redis unreachable")

    async def set(self, *args: Any, **kwargs: Any) -> bool:
        raise OSError("redis unreachable")

    async def delete(self, *keys: object) -> int:
        raise OSError("redis unreachable")

    async def eval(self, *args: object) -> list[int]:
        raise OSError("redis unreachable")

    async def scan_iter(self, **kwargs: Any):
        raise OSError("redis unreachable")
        yield ""  # pragma: no cover - generator contract only

    async def aclose(self) -> None:
        return None


async def drill_fraud_model_missing() -> DrillResult:
    """Remove the primary artifact path: scoring must degrade, not stop."""

    scorer = ResilientFraudScorer(
        PROJECT_ROOT / "models" / "fraud" / "does_not_exist.joblib",
        PROJECT_ROOT / "models" / "fraud" / "also_missing.joblib",
    )
    frame = pd.DataFrame(
        [
            {
                "is_proxy_ip": 1,
                "failed_attempts_24h": 4,
                "is_new_device": 1,
                "ip_risk_score": 0.9,
                "account_changes_24h": 2,
                "txn_velocity_1h": 12,
                "prior_disputes_90d": 0,
                "billing_shipping_mismatch": 0,
            }
        ]
    )
    scored = scorer.score(frame)
    passed = bool(scored["degraded"]) and float(scored["risk_probability"][0]) > 0
    return DrillResult(
        name="fraud_model_missing",
        scenario="Primary fraud model artifact and anomaly fallback both unavailable",
        expected="Conservative deterministic rule scoring continues; degraded flag and reason set",
        observed=(
            f"degraded={scored['degraded']}, score_space={scored['score_space']}, "
            f"risk={float(scored['risk_probability'][0]):.2f}"
        ),
        passed=passed,
        evidence={"reason": scored["reason"], "score_space": scored["score_space"]},
    )


async def drill_redis_down() -> DrillResult:
    """Redis unreachable: the cache must serve from the local fallback and mark degraded."""

    state = DegradationState()
    cache = CacheService(BrokenRedis(), settings=_settings(), state=state)
    await cache.ping()
    await cache.set_prediction("DRILL-TXN", {"risk_probability": 0.5}, ttl_seconds=60)
    value = await cache.get_prediction("DRILL-TXN")
    health = state.get("redis")
    passed = value is not None and health.status in {"degraded", "down"}
    await cache.close()
    return DrillResult(
        name="redis_down",
        scenario="Redis refuses every operation mid-run",
        expected="Requests still succeed from the process-local fallback; redis marked degraded",
        observed=f"redis={health.status}, cached_read={'hit' if value else 'miss'}",
        passed=passed,
        evidence={"reason": health.reason, "stats": cache.stats()},
    )


async def drill_llm_unavailable() -> DrillResult:
    """All model tiers fail: the deterministic template must still produce analyst output."""

    state = DegradationState()
    settings = _settings().model_copy(update={"gemini_api_key": "drill-key"})
    cache = CacheService(BrokenRedis(), settings=settings, state=state)

    async def always_fails(*_: Any, **__: Any) -> Any:
        raise TimeoutError("model endpoint unavailable")

    gateway = StructuredLLMGateway(cache, settings, invoker=always_fails, state=state)
    result = await gateway.generate(
        ModelTier.PRIMARY,
        "Summarise the incident for an analyst.",
        {"incident": "DRILL"},
        AlertExplanation,
        lambda: AlertExplanation(
            title="Deterministic incident summary",
            analyst_summary="Template summary built from detector output.",
            next_step="Review ranked responses.",
        ),
        prompt_version="drill-v1",
        incident_id="INC-DRILL",
        trace_id="trace-drill",
    )
    health = state.get("llm")
    passed = result.degraded and result.output.title and health.status != "healthy"
    await cache.close()
    return DrillResult(
        name="llm_unavailable",
        scenario="Every model tier times out (equivalent to a revoked API key)",
        expected="Deterministic template returns usable output; llm marked degraded",
        observed=f"degraded={result.degraded}, llm={health.status}, title={result.output.title!r}",
        passed=bool(passed),
        evidence={"failure_reasons": list(result.failure_reasons), "model": result.model_name},
    )


async def drill_stream_down() -> DrillResult:
    """Broker unreachable: producer start must fail loudly and mark the stream down."""

    state = DegradationState()
    settings = _settings().model_copy(
        update={"redpanda_bootstrap_servers": "127.0.0.1:1"}
    )

    class UnreachableProducer:
        async def start(self) -> None:
            raise OSError("connection refused")

        async def stop(self) -> None:
            return None

    producer = EventProducer(settings, producer=UnreachableProducer(), state=state)
    error: AppError | None = None
    try:
        await producer.start()
    except AppError as exc:
        error = exc
    health = state.get("stream")
    passed = error is not None and health.status == "down"
    return DrillResult(
        name="stream_down",
        scenario="Redpanda refuses connections when the producer starts",
        expected="Explicit stream_unavailable error and stream marked down; API keeps serving",
        observed=f"error={error.code if error else None}, stream={health.status}",
        passed=passed,
        evidence={"reason": health.reason},
    )


async def drill_policy_violation() -> DrillResult:
    """A broad defensive rule above the legitimate-value ceiling must be denied."""

    engine = PolicyEngine.default()
    decision = engine.evaluate(
        "temporary_defensive_rule",
        PolicyContext(
            affected_legitimate_value_inr=5_000_000,
            fraud_exposure_inr=250_000,
            segment_breadth=0.6,
            grounding_score=1.0,
            confidence_score=1.0,
            novelty_score=0.2,
            actor_role="lead_analyst",
        ),
    )
    return DrillResult(
        name="policy_violation",
        scenario="AI-recommended broad defensive rule exceeds the legitimate-value ceiling",
        expected="Deterministic policy denies the action before execution",
        observed=f"decision={decision.decision}, rule={decision.rule_id}",
        passed=decision.decision == "deny",
        evidence={"reason": decision.reason, "policy_version": decision.policy_version},
    )


async def drill_verification_rejection() -> DrillResult:
    """A claim citing unresolvable evidence must be stripped from the analyst view."""

    evidence = [{"evidence_id": "EV-1"}, {"evidence_id": "EV-2"}]
    claims = [
        {"claim_id": "C1", "statement": "Grounded claim", "evidence_ids": ["EV-1"]},
        {"claim_id": "C2", "statement": "Hallucinated citation", "evidence_ids": ["EV-999"]},
    ]
    verification = {
        "verdicts": [
            {"claim_id": "C1", "verdict": "supported"},
            {"claim_id": "C2", "verdict": "supported"},
        ]
    }
    result = ground_claims(claims, evidence, verification)
    passed = result["supported_claim_count"] == 1 and result["rejected_claim_count"] == 1
    return DrillResult(
        name="verification_rejection",
        scenario="Model cites an evidence ID that does not exist in the evidence store",
        expected="Claim stripped, counted, and grounding score reduced",
        observed=(
            f"supported={result['supported_claim_count']}, "
            f"rejected={result['rejected_claim_count']}, "
            f"grounding={result['grounding_score']:.2f}"
        ),
        passed=passed,
        evidence={"rejected_claims": result["rejected_claims"]},
    )


async def drill_underperforming_candidate() -> DrillResult:
    """A candidate with better reward but a safety violation must not be promotable."""

    gate = PromotionGate(reward_margin_inr=0.0, recall_tolerance=0.02, fp_cost_tolerance=0.05)
    production = PolicyMetrics(
        expected_reward_inr=1_000,
        precision=0.9,
        recall=0.8,
        false_positive_cost_inr=100,
        fraud_value_captured_inr=5_000,
        escalation_rate=0.4,
        safety_violations=0,
        evaluated_incidents=10,
    )
    candidate = PolicyMetrics(
        expected_reward_inr=5_000,
        precision=0.7,
        recall=0.6,
        false_positive_cost_inr=900,
        fraud_value_captured_inr=9_000,
        escalation_rate=0.0,
        safety_violations=2,
        evaluated_incidents=10,
    )
    result = gate.evaluate(candidate, production)
    passed = not result.passed and "zero_safety_violations" in result.reasons
    return DrillResult(
        name="underperforming_candidate_policy",
        scenario="Candidate policy has higher reward but safety violations and worse recall",
        expected="Promotion gate blocks it; promotion stays an explicit admin action",
        observed=f"passed={result.passed}, reasons={result.reasons}",
        passed=passed,
        evidence={"checks": result.checks},
    )


async def drill_postgres_down() -> DrillResult:
    """Postgres refuses writes: ingestion must buffer to the stream and return an explicit 503."""

    from sqlalchemy.exc import OperationalError

    from backend.app.services.transaction_service import TransactionService

    class FailingSession:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def scalars(self, *_: object, **__: object) -> Any:
            raise OperationalError("SELECT 1", {}, Exception("postgres unavailable"))

        async def execute(self, *_: object, **__: object) -> Any:
            raise OperationalError("SELECT 1", {}, Exception("postgres unavailable"))

        async def commit(self) -> None:
            raise OperationalError("COMMIT", {}, Exception("postgres unavailable"))

        def add(self, _: object) -> None:
            return None

    buffered: list[str] = []

    class BufferProducer:
        async def send(
            self, topic: str, event_type: str, payload: dict[str, Any], **_: Any
        ) -> None:
            buffered.append(event_type)

    state = DegradationState()
    settings = _settings()
    cache = CacheService(BrokenRedis(), settings=settings, state=state)
    service = TransactionService(
        settings,
        cache=cache,
        session_factory=lambda: FailingSession(),
        publisher=BufferProducer(),
        state=state,
    )
    frame = load_features("validation", settings.data_dir)
    await service.initialize(frame.head(2_000))

    error: AppError | None = None
    try:
        await service.ingest(frame.iloc[2_000].to_dict())
    except AppError as exc:
        error = exc
    health = state.get("postgres")
    passed = (
        error is not None
        and error.code == "database_unavailable"
        and error.http_status == 503
        and health.status == "down"
        and buffered == ["transaction.buffered"]
    )
    await cache.close()
    return DrillResult(
        name="postgres_down",
        scenario="Postgres refuses the transaction write during ingestion",
        expected="Explicit 503 database_unavailable, postgres marked down, transaction buffered to the stream",
        observed=(
            f"error={error.code if error else None}/{error.http_status if error else None}, "
            f"postgres={health.status}, buffered={buffered}"
        ),
        passed=passed,
        evidence={"reason": health.reason, "buffered_events": buffered},
    )


async def drill_shadow_candidate_isolation() -> DrillResult:
    """A corrupt shadow candidate must never block the operative production ranking."""

    class BrokenProduction:
        def rank(self, _: Any) -> list[dict[str, Any]]:
            raise ValueError("corrupt production artifact")

    result = ShadowPolicy(BrokenProduction()).score({})  # type: ignore[arg-type]
    passed = result["operative_action"] == "human_escalation" and result["degraded"] is True
    return DrillResult(
        name="policy_artifact_corrupt",
        scenario="Production response-policy artifact fails to score",
        expected="Conservative ranking headed by human_escalation; degradation visible",
        observed=f"operative={result['operative_action']}, degraded={result['degraded']}",
        passed=passed,
        evidence={"production_error": result["production_error"]},
    )


DRILLS = {
    "fraud_model_missing": drill_fraud_model_missing,
    "redis_down": drill_redis_down,
    "llm_unavailable": drill_llm_unavailable,
    "stream_down": drill_stream_down,
    "postgres_down": drill_postgres_down,
    "policy_violation": drill_policy_violation,
    "verification_rejection": drill_verification_rejection,
    "underperforming_candidate_policy": drill_underperforming_candidate,
    "policy_artifact_corrupt": drill_shadow_candidate_isolation,
}


def _markdown(results: list[DrillResult]) -> str:
    passed = sum(1 for item in results if item.passed)
    lines = [
        "# Failure Drill Checklist",
        "",
        f"{passed}/{len(results)} drills produced their expected visible degraded state.",
        "",
        "Every drill runs in-process, so this checklist is reproducible without Docker, a broker,",
        "or model credentials. Nothing here fails silently: each case sets a dependency state, a",
        "Prometheus counter, or an explicit error.",
        "",
        "| Drill | Expected | Observed | Result |",
        "|---|---|---|:--:|",
    ]
    for item in results:
        lines.append(
            f"| `{item.name}` | {item.expected} | {item.observed} | "
            f"{'pass' if item.passed else 'FAIL'} |"
        )
    lines += ["", "## Scenarios", ""]
    for item in results:
        lines += [
            f"### {item.name}",
            "",
            f"- Scenario: {item.scenario}",
            f"- Expected: {item.expected}",
            f"- Observed: {item.observed}",
            f"- Evidence: `{json.dumps(item.evidence, default=str)}`",
            "",
        ]
    return "\n".join(lines)


async def run(names: list[str]) -> int:
    results = [await DRILLS[name]() for name in names]
    output_dir = PROJECT_ROOT / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "FAILURE_DRILLS.md").write_text(_markdown(results), encoding="utf-8")
    (output_dir / "metrics" / "failure_drills.json").write_text(
        json.dumps([item.to_dict() for item in results], indent=2), encoding="utf-8"
    )
    failed = [item.name for item in results if not item.passed]
    print(
        json.dumps(
            {
                "drills": len(results),
                "passed": len(results) - len(failed),
                "failed": failed,
                "checklist": "reports/FAILURE_DRILLS.md",
            },
            indent=2,
        )
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List drill names and exit")
    parser.add_argument("--only", action="append", choices=sorted(DRILLS), help="Run one drill")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(sorted(DRILLS), indent=2))
        return 0
    return asyncio.run(run(args.only or sorted(DRILLS)))


if __name__ == "__main__":
    raise SystemExit(main())
