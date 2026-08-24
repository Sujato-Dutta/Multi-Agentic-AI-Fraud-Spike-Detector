from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.config import Settings
from backend.app.core.runtime import AppError
from backend.app.db.models import AuditEvent, Base
from backend.app.db.repositories import LearningRepository
from backend.app.ml.artifacts import sha256_file
from backend.app.ml.policy.contextual_bandit import ACTIONS, LinUCBPolicy
from backend.app.ml.policy.shadow_policy import (
    PolicyMetrics,
    PromotionGate,
    ShadowPolicy,
)
from backend.app.ml.reward.reward_model import CONTEXT_FEATURES
from backend.app.schemas import UserIdentity
from backend.app.services.policy_service import (
    BUILTIN_CONSERVATIVE_IDENTITY,
    PolicyService,
    RuntimePolicyResolver,
    build_promotion_evidence,
)

pytestmark = pytest.mark.asyncio
ADMIN = UserIdentity(username="admin", role="admin")


def _metrics(**overrides) -> dict:
    values = {
        "expected_reward_inr": 1_000.0,
        "precision": 0.80,
        "recall": 0.75,
        "false_positive_cost_inr": 100.0,
        "fraud_value_captured_inr": 5_000.0,
        "escalation_rate": 0.20,
        "safety_violations": 0,
        "evaluated_incidents": 10,
    }
    values.update(overrides)
    return values


def _gate() -> PromotionGate:
    return PromotionGate(
        reward_margin_inr=50.0,
        recall_tolerance=0.02,
        fp_cost_tolerance=0.05,
    )


def _context() -> dict[str, float]:
    return {name: 1.0 for name in CONTEXT_FEATURES}


def _artifact(tmp_path: Path, name: str, preferred: str) -> tuple[Path, str]:
    model = LinUCBPolicy(alpha=0).fit(
        [_context()] * len(ACTIONS),
        list(ACTIONS),
        [10_000.0 if action == preferred else 0.0 for action in ACTIONS],
        assumptions_version="action-effects-v1",
    )
    path = model.save(tmp_path / name)
    return path, sha256_file(path)


def _evidence(
    checksum: str,
    *,
    candidate_metrics: dict | None = None,
    candidate_version: int = 2,
    production_version: int = 1,
) -> dict:
    return {
        "promotion_evidence": build_promotion_evidence(
            candidate_metrics=candidate_metrics or _metrics(expected_reward_inr=1_100.0),
            production_metrics=_metrics(),
            holdback={
                "incidents": ["VAL_S2"],
                "selection": "chronological_validation_tail_frozen_before_scoring",
            },
            assumptions_version="action-effects-v1",
            candidate_checksum=checksum,
            production_identity=BUILTIN_CONSERVATIVE_IDENTITY,
            candidate_version=candidate_version,
            production_version=production_version,
        )
    }


async def _stack(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        _env_file=None,
        policy_reward_margin_inr=50.0,
        policy_recall_tolerance=0.02,
        policy_fp_cost_tolerance=0.05,
    )
    return engine, factory, PolicyService(session_factory=factory, settings=settings)


async def _seed(factory, tmp_path, *, candidate_metrics: dict | None = None):
    artifact, checksum = _artifact(
        tmp_path, "candidate.joblib", "step_up_verification"
    )
    async with factory() as session:
        repository = LearningRepository(session)
        production = await repository.create_policy_version(
            version=1,
            rules={"family": "fixed_human_escalation"},
            metrics=_metrics(),
            artifact_checksum=BUILTIN_CONSERVATIVE_IDENTITY,
            created_by="system",
        )
        await repository.activate_version(production.policy_version_id, "system")
        candidate = await repository.create_policy_version(
            version=2,
            rules={"family": "LinUCB", "live_exploration": False},
            metrics=_evidence(checksum, candidate_metrics=candidate_metrics),
            artifact_uri=str(artifact),
            artifact_checksum=checksum,
            created_by="trainer",
            status="shadow",
            parent_version=1,
        )
        await session.commit()
    return production, candidate


async def test_safety_violation_blocks_better_candidate() -> None:
    production = PolicyMetrics.model_validate(_metrics())
    candidate = PolicyMetrics.model_validate(
        _metrics(expected_reward_inr=2_000.0, safety_violations=1)
    )
    result = _gate().evaluate(candidate, production)
    assert result.passed is False
    assert result.checks["zero_safety_violations"] is False


async def test_recall_or_false_positive_regression_blocks_promotion() -> None:
    production = PolicyMetrics.model_validate(_metrics())
    assert not _gate().evaluate(
        PolicyMetrics.model_validate(_metrics(expected_reward_inr=2_000, recall=0.70)),
        production,
    ).passed
    assert not _gate().evaluate(
        PolicyMetrics.model_validate(
            _metrics(expected_reward_inr=2_000, false_positive_cost_inr=106)
        ),
        production,
    ).passed


async def test_promotion_requires_admin_is_audited_and_changes_serving(tmp_path) -> None:
    engine, factory, service = await _stack(tmp_path)
    try:
        _, candidate = await _seed(factory, tmp_path)
        runtime_a = RuntimePolicyResolver(
            session_factory=factory, assumptions_version="action-effects-v1"
        )
        runtime_b = RuntimePolicyResolver(
            session_factory=factory, assumptions_version="action-effects-v1"
        )
        assert (await runtime_a.score(_context()))["operative_action"] == "human_escalation"
        with pytest.raises(AppError, match="Admin approval"):
            await service.promote(
                candidate.policy_version_id,
                UserIdentity(username="analyst", role="analyst"),
            )
        promoted = await service.promote(candidate.policy_version_id, ADMIN)
        assert promoted["status"] == "production"
        assert (await runtime_a.score(_context()))["operative_action"] == "step_up_verification"
        assert (await runtime_b.score(_context()))["operative_action"] == "step_up_verification"
        async with factory() as session:
            events = list(
                (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.event_type == "response_policy_promoted"
                        )
                    )
                ).all()
            )
        assert len(events) == 1
        assert events[0].payload["holdback_digest"]
    finally:
        await engine.dispose()


async def test_failed_gate_or_stale_evidence_never_activates_candidate(tmp_path) -> None:
    engine, factory, service = await _stack(tmp_path)
    try:
        _, candidate = await _seed(
            factory,
            tmp_path,
            candidate_metrics=_metrics(expected_reward_inr=2_000, safety_violations=1),
        )
        with pytest.raises(AppError, match="zero_safety_violations"):
            await service.promote(candidate.policy_version_id, ADMIN)
        async with factory() as session:
            repository = LearningRepository(session)
            assert (await repository.active_policy_version()).version == 1
            stored_candidate = await repository.get_policy_version(
                candidate.policy_version_id
            )
            metrics = dict(stored_candidate.metrics)
            evidence = dict(metrics["promotion_evidence"])
            evidence["production_identity"] = "stale"
            metrics["promotion_evidence"] = evidence
            stored_candidate.metrics = metrics
            await session.commit()
        with pytest.raises(AppError, match="Invalid promotion evidence"):
            await service.promote(candidate.policy_version_id, ADMIN)
    finally:
        await engine.dispose()


async def test_rollback_restores_prior_version_and_runtime(tmp_path) -> None:
    engine, factory, service = await _stack(tmp_path)
    try:
        first, second = await _seed(factory, tmp_path)
        await service.promote(second.policy_version_id, ADMIN)
        restored = await service.rollback(first.policy_version_id, ADMIN)
        assert restored["version"] == 1 and restored["status"] == "production"
        runtime = RuntimePolicyResolver(
            session_factory=factory, assumptions_version="action-effects-v1"
        )
        result = await runtime.score(_context())
        assert result["operative_action"] == "human_escalation"
        assert result["degraded"] is False
        async with factory() as session:
            events = list(
                (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.event_type == "response_policy_rolled_back"
                        )
                    )
                ).all()
            )
        assert len(events) == 1
    finally:
        await engine.dispose()


async def test_shadow_candidate_never_changes_or_blocks_operative_action() -> None:
    production = LinUCBPolicy(alpha=0).fit(
        [_context()] * len(ACTIONS),
        list(ACTIONS),
        [100.0 if action == "no_action" else 0.0 for action in ACTIONS],
        assumptions_version="action-effects-v1",
    )

    class BrokenCandidate:
        def rank(self, context):
            raise ValueError("corrupt shadow")

    result = ShadowPolicy(production, BrokenCandidate()).score(_context())  # type: ignore[arg-type]
    assert result["operative_action"] == "no_action"
    assert result["candidate_ranking"] is None
    assert result["candidate_shadow_only"] is True
    assert result["candidate_degraded"] is True
    assert result["degraded"] is False
    assert not hasattr(ShadowPolicy, "promote")
