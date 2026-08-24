"""Immutable analyst outcomes persisted with a durable publication outbox."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from backend.app.core.runtime import AppError
from backend.app.db.repositories import (
    AuditRepository,
    FeedbackRepository,
    IncidentRepository,
    OutboxRepository,
)
from backend.app.hitl.review_service import decision_dict
from backend.app.schemas import OutcomeRequest, UserIdentity
from backend.app.streaming.topics import TopicSet

Publisher = Any
LocalPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
logger = structlog.get_logger(__name__)


class FeedbackService:
    def __init__(
        self,
        *,
        session_factory: Any,
        publisher: Publisher | None = None,
        local_publisher: LocalPublisher | None = None,
        topics: TopicSet | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.local_publisher = local_publisher
        self.topics = topics

    async def record_outcome(
        self,
        decision_id: str,
        request: OutcomeRequest,
        actor: UserIdentity,
    ) -> dict[str, Any]:
        outcome = request.model_dump(mode="json")
        async with self.session_factory() as session:
            feedback = FeedbackRepository(session)
            row, recorded = await feedback.compare_and_set_outcome(decision_id, outcome)
            if row is None:
                raise AppError("decision_not_found", 404, "Analyst decision does not exist")
            if not recorded and row.outcome != outcome:
                if row.outcome is None:
                    raise AppError(
                        "decision_not_completed",
                        409,
                        "Outcome recording requires a completed analyst decision",
                    )
                raise AppError(
                    "outcome_already_recorded",
                    409,
                    "A different immutable outcome is already recorded",
                )
            if recorded:
                await IncidentRepository(session).set_status(row.incident_id, "completed")
                await AuditRepository(session).append_once(
                    incident_id=row.incident_id,
                    event_type="outcome_recorded",
                    actor=actor.username,
                    payload={"decision_id": decision_id, "outcome": outcome},
                    trace_id=None,
                    idempotency_key=f"outcome:{decision_id}",
                )
            result = decision_dict(row)
            if self.topics is not None:
                await OutboxRepository(session).enqueue_once(
                    event_id=f"outcome:{decision_id}",
                    topic=self.topics.outcomes,
                    event_type="analyst.outcome",
                    payload=result,
                    trace_id=row.incident_id,
                    message_key=row.incident_id,
                    occurred_at=row.outcome_recorded_at,
                )
            await session.commit()
        if self.local_publisher is not None:
            try:
                await self.local_publisher("audit_event", result)
            except Exception as exc:  # noqa: BLE001 - websocket is best effort
                logger.warning("outcome_websocket_failed", reason=str(exc)[:500])
        return result
