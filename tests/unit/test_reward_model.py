import asyncio
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.config import Settings
from backend.app.core.runtime import AppError, DegradationState
from backend.app.db.models import (
    AnalystDecision,
    Base,
    Incident,
    IncidentMemory,
    OutboxEvent,
    Reward,
)
from backend.app.hitl.feedback_service import FeedbackService
from backend.app.ml.artifacts import ArtifactIntegrityError
from backend.app.ml.reward.reward_model import CONTEXT_FEATURES, RewardModel
from backend.app.schemas import OutcomeRequest, UserIdentity
from backend.app.services.evaluation_service import (
    RESPONSE_ACTIONS,
    ActionEffects,
    EvaluationService,
    RewardCalculator,
)
from backend.app.streaming.outbox import OutboxDispatcher
from backend.app.streaming.topics import TopicSet


def _context(seed: float = 1.0) -> dict[str, float]:
    return {name: seed + index / 10 for index, name in enumerate(CONTEXT_FEATURES)}


def _calculator() -> RewardCalculator:
    effects = ActionEffects.from_yaml(Path("infrastructure/action_effects.yaml"))
    return RewardCalculator(effects, Settings(_env_file=None))


def test_reward_arithmetic_matches_hand_calculation() -> None:
    transactions = [
        {
            "transaction_id": "fraud",
            "customer_id": "fraud-customer",
            "is_fraud": 1,
            "fraud_loss_if_missed_inr": 1_000.0,
            "false_positive_cost_if_blocked_inr": 0.0,
        },
        {
            "transaction_id": "legit",
            "customer_id": "legit-customer",
            "is_fraud": 0,
            "fraud_loss_if_missed_inr": 0.0,
            "false_positive_cost_if_blocked_inr": 200.0,
        },
    ]
    result = _calculator().calculate("step_up_verification", transactions)
    assert result.fraud_prevented_inr == 700.0
    assert result.false_positive_cost_inr == 6.0
    assert result.friction_cost_inr == 40.0
    assert result.review_cost_inr == 12.5
    assert result.delay_cost_inr == 125.0
    assert result.total_reward_inr == 516.5


def test_broad_block_is_worse_than_targeted_step_up() -> None:
    transactions = [
        {
            "transaction_id": "fraud",
            "customer_id": "fraud",
            "is_fraud": 1,
            "fraud_loss_if_missed_inr": 1_000.0,
            "false_positive_cost_if_blocked_inr": 0.0,
        },
        *[
            {
                "transaction_id": f"legit-{index}",
                "customer_id": f"customer-{index}",
                "is_fraud": 0,
                "fraud_loss_if_missed_inr": 0.0,
                "false_positive_cost_if_blocked_inr": 500.0,
            }
            for index in range(10)
        ],
    ]
    calculator = _calculator()
    broad = calculator.calculate("temporary_defensive_rule", transactions)
    targeted = calculator.calculate("step_up_verification", transactions)
    assert broad.fraud_prevented_inr > targeted.fraud_prevented_inr
    assert broad.total_reward_inr < targeted.total_reward_inr


def test_all_actions_have_versioned_counterfactuals() -> None:
    rows = [
        {
            "transaction_id": "one",
            "customer_id": "one",
            "is_fraud": 1,
            "fraud_loss_if_missed_inr": 100.0,
            "false_positive_cost_if_blocked_inr": 0.0,
        }
    ]
    results = _calculator().counterfactuals(rows)
    assert tuple(result.action for result in results) == RESPONSE_ACTIONS
    assert {result.assumptions_version for result in results} == {"action-effects-v1"}
    assert all(
        result.assumptions_source == "explicit_counterfactual_assumptions"
        and result.currency == "INR"
        and result.assumptions_notice
        for result in results
    )
    assert all(result.assumptions for result in results)


def test_reward_model_predictions_survive_save_load(tmp_path) -> None:
    contexts = [_context(float(index + 1)) for index in range(18)]
    actions = [RESPONSE_ACTIONS[index % len(RESPONSE_ACTIONS)] for index in range(18)]
    rewards = [float(index * 100 - (index % 3) * 20) for index in range(18)]
    model = RewardModel(estimators=30, random_seed=7).fit(
        contexts, actions, rewards, assumptions_version="action-effects-v1"
    )
    expected = model.predict(contexts, actions)
    artifact = model.save(tmp_path / "reward.joblib")
    loaded = RewardModel.load(
        artifact,
        assumptions_version="action-effects-v1",
    )
    actual = loaded.predict(contexts, actions)
    assert np.isfinite(actual).all()
    assert np.allclose(expected, actual)
    assert len(loaded.rank(_context())) == 6
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        RewardModel.load(artifact, assumptions_version="action-effects-v1")


def test_invalid_assumptions_and_unknown_actions_fail_closed(tmp_path) -> None:
    payload = yaml.safe_load(Path("infrastructure/action_effects.yaml").read_text())
    payload["actions"].pop("no_action")
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        ActionEffects.from_yaml(path)
    with pytest.raises(ValueError, match="Unknown response action"):
        _calculator().calculate("invented_action", [])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authoritative_outcome_replay_is_atomic_and_outboxed(
    tmp_path, monkeypatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reward.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        outbox_poll_seconds=0.1,
        outbox_cycle_retry_max_seconds=0.1,
    )
    state = DegradationState()
    topics = TopicSet.from_settings(settings)
    timestamp = datetime(2026, 1, 1, 12, tzinfo=UTC).replace(tzinfo=None)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                Incident(
                    incident_id="INC-OUTCOME",
                    alert_id="ALERT-OUTCOME",
                    status="awaiting_outcome",
                    detected_at=timestamp,
                    window_start=timestamp,
                    window_end=timestamp,
                    reason="test",
                    detector_output={},
                )
            )
            session.add(
                AnalystDecision(
                    decision_id="DEC-OUTCOME",
                    incident_id="INC-OUTCOME",
                    actor_username="analyst",
                    decision="approve",
                    status="completed",
                    reason_code="confirmed_risk",
                    original_recommendation={
                        "recommendation": {
                            "action": "step_up_verification",
                            "conditions": ["payment_method=card"],
                            "amount_band": "medium",
                        }
                    },
                    final_action={"action": "step_up_verification"},
                    decided_at=timestamp,
                )
            )
            await session.commit()
        feedback = FeedbackService(session_factory=factory, topics=topics)
        request = OutcomeRequest(
            outcome_code="prevented_loss",
            fraud_loss_inr=1_000,
            false_positive_cost_inr=10,
        )
        canonical = await feedback.record_outcome(
            "DEC-OUTCOME",
            request,
            UserIdentity(username="analyst", role="analyst"),
        )
        evaluation = EvaluationService(
            session_factory=factory,
            calculator=_calculator(),
            topics=topics,
        )
        tampered = {**canonical, "incident_id": "INC-TAMPERED"}
        with pytest.raises(ValueError, match="authoritative decision"):
            await evaluation.handle_outcome(tampered, "ignored")
        first = await evaluation.handle_outcome(canonical, "ignored")
        second = await evaluation.handle_outcome(canonical, "ignored")
        assert first["created"] is True and second["created"] is False
        with pytest.raises(AppError, match="different immutable outcome"):
            await feedback.record_outcome(
                "DEC-OUTCOME",
                OutcomeRequest(outcome_code="legitimate"),
                UserIdentity(username="analyst", role="analyst"),
            )
        async with factory() as session:
            reward_count = await session.scalar(
                select(func.count()).select_from(Reward)
            )
            memory_count = await session.scalar(
                select(func.count()).select_from(IncidentMemory)
            )
            outbox_rows = list(
                (await session.scalars(select(OutboxEvent).order_by(OutboxEvent.event_id))).all()
            )
        assert reward_count == 1 and memory_count == 1
        assert [row.event_id for row in outbox_rows] == [
            "outcome:DEC-OUTCOME",
            "reward:outcome:DEC-OUTCOME:action-effects-v1",
        ]
        assert all(row.status == "pending" for row in outbox_rows)

        class FailingProducer:
            async def send_envelope(self, topic, envelope, *, key=None):
                raise OSError("redpanda unavailable")

        failed_dispatch = OutboxDispatcher(
            session_factory=factory,
            producer=FailingProducer(),  # type: ignore[arg-type]
            settings=settings,
            state=state,
        )
        assert await failed_dispatch.drain_once() == 0
        assert state.postgres.status == "healthy"
        async with factory() as session:
            failed = list((await session.scalars(select(OutboxEvent))).all())
            assert all(row.status == "pending" and row.last_error for row in failed)
            await session.execute(
                update(OutboxEvent).values(
                    available_at=datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None)
                )
            )
            await session.commit()

        class RecordingProducer:
            def __init__(self):
                self.ids = []

            async def send_envelope(self, topic, envelope, *, key=None):
                self.ids.append(envelope.event_id)
                return envelope

        recorder = RecordingProducer()
        recovered_dispatch = OutboxDispatcher(
            session_factory=factory,
            producer=recorder,  # type: ignore[arg-type]
            settings=settings,
            state=state,
        )
        assert await recovered_dispatch.drain_once() == 2
        assert recorder.ids == [row.event_id for row in outbox_rows]
        assert await recovered_dispatch.drain_once() == 0
        async with factory() as session:
            published = list((await session.scalars(select(OutboxEvent))).all())
        assert all(row.status == "published" for row in published)

        cycle_state = DegradationState()

        class RecoveringFactory:
            def __init__(self):
                self.calls = 0
                self.observed_statuses = []

            def __call__(self):
                self.calls += 1
                self.observed_statuses.append(cycle_state.postgres.status)
                if self.calls <= 3:
                    raise OSError("[WinError 1225] connection refused")
                return factory()

        class RecordingLogger:
            def __init__(self):
                self.warnings = []
                self.infos = []

            def warning(self, event, **values):
                self.warnings.append((event, values))

            def info(self, event, **values):
                self.infos.append((event, values))

        recovering_factory = RecoveringFactory()
        recording_logger = RecordingLogger()
        monkeypatch.setattr("backend.app.streaming.outbox.logger", recording_logger)
        cycle_dispatch = OutboxDispatcher(
            session_factory=recovering_factory,
            producer=recorder,  # type: ignore[arg-type]
            settings=settings,
            state=cycle_state,
        )
        cycle_task = asyncio.create_task(cycle_dispatch.run())
        try:
            for _ in range(100):
                if recovering_factory.calls >= 4 and cycle_state.postgres.status == "healthy":
                    break
                await asyncio.sleep(0.01)
        finally:
            await cycle_dispatch.stop()
            await cycle_task
        assert recovering_factory.calls >= 4
        assert "down" in recovering_factory.observed_statuses
        assert cycle_state.postgres.status == "healthy"
        assert [event for event, _ in recording_logger.warnings] == [
            "outbox_cycle_failed"
        ]
        assert recording_logger.warnings[0][1]["dependency"] == "postgres"
        assert [event for event, _ in recording_logger.infos] == [
            "outbox_cycle_recovered"
        ]
        assert recording_logger.infos[0][1]["failed_cycles"] == 3
    finally:
        await engine.dispose()
