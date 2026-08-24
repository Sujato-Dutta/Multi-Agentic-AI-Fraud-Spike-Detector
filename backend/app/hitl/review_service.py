"""Transactional analyst-review boundary around checkpoint resume."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter
from typing import Any

import structlog
from prometheus_client import Counter

from backend.app.core.runtime import AppError
from backend.app.db.repositories import (
    AuditRepository,
    FeedbackRepository,
    OutboxRepository,
)
from backend.app.hitl.approval_rules import resolve_final_action
from backend.app.monitoring.prometheus import DECISION_LATENCY
from backend.app.safety.evidence_grounding import build_authorization_context
from backend.app.safety.metrics import record_degradation
from backend.app.safety.permissions import allowed_actions
from backend.app.safety.policy_engine import PolicyEngine
from backend.app.schemas import ReviewDecisionRequest, UserIdentity
from backend.app.streaming.topics import TopicSet

REVIEW_DECISIONS = Counter(
    "fraud_review_decisions_total",
    "Human review choices and outcomes.",
    ("decision", "status"),
)
_RESUME_LEASE_SECONDS = 900
Publisher = Any
LocalPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
logger = structlog.get_logger(__name__)


class ReviewService:
    def __init__(
        self,
        investigation_service: Any,
        policy_engine: PolicyEngine,
        *,
        session_factory: Any,
        publisher: Publisher | None = None,
        local_publisher: LocalPublisher | None = None,
        topics: TopicSet | None = None,
    ) -> None:
        self.investigation = investigation_service
        self.policy = policy_engine
        self.session_factory = session_factory
        self.publisher = publisher
        self.local_publisher = local_publisher
        self.topics = topics
        self._decision_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def pending_review(self, incident_id: str, actor: UserIdentity) -> dict[str, Any]:
        state = await self._review_state(incident_id, pending_only=True)
        return {
            "incident_id": incident_id,
            "status": state["status"],
            "recommendation": state["responses"][0],
            "responses": state["responses"],
            "grounded_claims": state["grounded_claims"],
            "impact": state["impact"],
            "policy_gate": state["policy_gate"],
            "allowed_actions": allowed_actions(actor.role),
            "choices": ["approve", "modify", "reject", "escalate"],
            "checkpoint_durable": self.investigation.checkpoint_durable,
        }

    async def decide(
        self,
        incident_id: str,
        request: ReviewDecisionRequest,
        actor: UserIdentity,
    ) -> dict[str, Any]:
        decision_id = _decision_id(incident_id)
        started = perf_counter()
        async with self._decision_locks[decision_id]:
            try:
                return await self._decide_locked(
                    incident_id, decision_id, request, actor
                )
            finally:
                DECISION_LATENCY.observe(perf_counter() - started)

    async def _decide_locked(
        self,
        incident_id: str,
        decision_id: str,
        request: ReviewDecisionRequest,
        actor: UserIdentity,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            existing = await FeedbackRepository(session).get(decision_id)
        if existing is not None and not _is_in_flight(existing.status):
            self._ensure_same_request(existing, actor, request)
            result = {
                **decision_dict(existing),
                "investigation_status": "awaiting_outcome",
            }
            await self._ensure_decision_outbox(existing, result)
            await self._broadcast(result)
            return result

        state = await self._review_state(
            incident_id, pending_only=existing is None
        )
        if existing is None:
            if (
                request.decision in {"approve", "modify"}
                and not self.investigation.checkpoint_durable
            ):
                raise AppError(
                    "checkpoint_not_durable",
                    503,
                    "Approval is disabled while durable checkpoints are unavailable",
                )
            row = await self._record_decision(
                incident_id, decision_id, request, actor, state
            )
        else:
            self._ensure_same_request(existing, actor, request)
            row = existing

        row, owner_status = await self._claim_resume(row, actor, request)
        if owner_status is None:
            return decision_dict(row)
        review = _review_payload(row)
        try:
            current = await self._review_state(incident_id, pending_only=False)
            if current.get("status") == "awaiting_human_review":
                resumed = await self.investigation.resume(incident_id, review)
            else:
                self._ensure_matching_checkpoint_review(current, review)
                if current.get("status") == "awaiting_outcome":
                    resumed = await self.investigation.reconcile(current)
                else:
                    resumed = await self.investigation.continue_from_checkpoint(
                        incident_id
                    )
            if resumed.get("status") != "awaiting_outcome":
                raise AppError(
                    "review_recovery_incomplete",
                    503,
                    "The accepted review has not reached the durable outcome checkpoint",
                )
            row = await self._complete_resume(
                row,
                owner_status,
                resumed,
                trace_id=state.get("trace_id"),
            )
        except BaseException:
            await asyncio.shield(self._release_claim(row.decision_id, owner_status))
            raise

        result = {**decision_dict(row), "investigation_status": resumed.get("status")}
        REVIEW_DECISIONS.labels(decision=row.decision, status="completed").inc()
        await self._broadcast(result)
        return result

    async def _record_decision(
        self,
        incident_id: str,
        decision_id: str,
        request: ReviewDecisionRequest,
        actor: UserIdentity,
        state: Mapping[str, Any],
    ) -> Any:
        recommendation = state["responses"][0]
        action = resolve_final_action(
            request.decision, recommendation, request.modified_action
        )
        policy_context, policy_basis = build_authorization_context(
            action, state["evidence"], state["impact"], actor_role=actor.role
        )
        policy = self.policy.evaluate(action, policy_context)
        if request.decision in {"approve", "modify"} and policy.decision == "deny":
            REVIEW_DECISIONS.labels(decision=request.decision, status="blocked").inc()
            record_degradation("policy_violation")
            raise AppError("policy_violation", 409, policy.reason)
        authorized = bool(
            request.decision in {"approve", "modify"}
            and policy.decision in {"allow", "require_approval"}
        )
        original = {
            "recommendation": recommendation,
            "policy_gate": state["policy_gate"],
            "initial_policy_context": state.get("policy_context", {}),
            "grounding": state["grounded_claims"],
        }
        final_action = {
            "action": action,
            "authorized": authorized,
            "authorization_source": "human_policy_gate" if authorized else "none",
            "actor_role": actor.role,
            "policy_context": policy_context.model_dump(mode="json"),
            "authorization_basis": policy_basis,
            "policy": policy.model_dump(mode="json"),
        }
        async with self.session_factory() as session:
            feedback = FeedbackRepository(session)
            row, created = await feedback.record_analyst_feedback(
                decision_id=decision_id,
                incident_id=incident_id,
                actor_username=actor.username,
                decision=request.decision,
                reason_code=request.reason_code,
                reason_text=request.reason_text,
                original_recommendation=original,
                final_action=final_action,
                status="resume_pending",
            )
            if not created:
                self._ensure_same_request(row, actor, request)
            else:
                await AuditRepository(session).append_once(
                    incident_id=incident_id,
                    event_type="analyst_decision_recorded",
                    actor=actor.username,
                    payload={
                        "decision_id": decision_id,
                        "decision": request.decision,
                        "reason_code": request.reason_code,
                        "final_action": final_action,
                    },
                    trace_id=state.get("trace_id"),
                    idempotency_key=f"decision:{decision_id}",
                )
            await session.commit()
            return row

    async def _claim_resume(
        self,
        row: Any,
        actor: UserIdentity,
        request: ReviewDecisionRequest,
    ) -> tuple[Any, str | None]:
        for _ in range(3):
            async with self.session_factory() as session:
                feedback = FeedbackRepository(session)
                current = await feedback.get(row.decision_id)
                if current is None:
                    raise AppError(
                        "decision_not_found", 404, "Analyst decision does not exist"
                    )
                self._ensure_same_request(current, actor, request)
                if not _is_in_flight(current.status):
                    return current, None
                if current.status != "resume_pending" and not _lease_expired(
                    current.status
                ):
                    raise AppError(
                        "review_in_progress",
                        409,
                        "Another worker is completing this analyst decision",
                    )
                owner_status = _new_owner_status()
                claimed = await feedback.compare_and_set_status(
                    current.decision_id, current.status, owner_status
                )
                if claimed:
                    await session.commit()
                    return current, owner_status
                await session.rollback()
        raise AppError(
            "review_claim_conflict",
            409,
            "The analyst decision changed while resume ownership was claimed",
        )

    async def _complete_resume(
        self,
        row: Any,
        owner_status: str,
        resumed: Mapping[str, Any],
        *,
        trace_id: str | None,
    ) -> Any:
        async with self.session_factory() as session:
            feedback = FeedbackRepository(session)
            completed = await feedback.compare_and_set_status(
                row.decision_id, owner_status, "completed"
            )
            if not completed:
                raise AppError(
                    "review_ownership_lost",
                    409,
                    "Resume ownership expired before completion",
                )
            await AuditRepository(session).append_once(
                incident_id=row.incident_id,
                event_type="investigation_resumed",
                actor=row.actor_username,
                payload={
                    "decision_id": row.decision_id,
                    "investigation_status": resumed.get("status"),
                    "authorized": bool(row.final_action.get("authorized", False)),
                },
                trace_id=trace_id,
                idempotency_key=f"resume:{row.decision_id}",
            )
            completed_row = await feedback.get(row.decision_id)
            assert completed_row is not None
            if self.topics is not None:
                await OutboxRepository(session).enqueue_once(
                    event_id=f"decision:{row.decision_id}",
                    topic=self.topics.analyst_actions,
                    event_type="analyst.decision",
                    payload={
                        **decision_dict(completed_row),
                        "investigation_status": resumed.get("status"),
                    },
                    trace_id=row.incident_id,
                    message_key=row.incident_id,
                )
            await session.commit()
            return completed_row

    async def _release_claim(self, decision_id: str, owner_status: str) -> None:
        async with self.session_factory() as session:
            await FeedbackRepository(session).compare_and_set_status(
                decision_id, owner_status, "resume_pending"
            )
            await session.commit()

    async def _review_state(
        self, incident_id: str, *, pending_only: bool
    ) -> dict[str, Any]:
        try:
            state = await self.investigation.get_state(incident_id)
        except ValueError as exc:
            raise AppError("review_not_found", 404, str(exc)) from exc
        if pending_only and state.get("status") != "awaiting_human_review":
            raise AppError("review_not_pending", 409, "Incident is not awaiting human review")
        required = {
            "responses",
            "grounded_claims",
            "policy_context",
            "policy_gate",
            "evidence",
            "impact",
        }
        if not required.issubset(state):
            raise AppError("review_state_invalid", 409, "Review checkpoint is incomplete")
        return dict(state)

    @staticmethod
    def _ensure_matching_checkpoint_review(
        state: Mapping[str, Any], review: Mapping[str, Any]
    ) -> None:
        stored = state.get("human_review")
        if not isinstance(stored, Mapping) or any(
            stored.get(key) != value for key, value in review.items()
        ):
            raise AppError(
                "review_checkpoint_conflict",
                409,
                "The durable checkpoint contains a different analyst review",
            )

    @staticmethod
    def _ensure_same_request(
        row: Any, actor: UserIdentity, request: ReviewDecisionRequest
    ) -> None:
        recommendation = row.original_recommendation.get("recommendation", {})
        action = resolve_final_action(
            request.decision, recommendation, request.modified_action
        )
        if (
            row.actor_username != actor.username
            or row.decision != request.decision
            or row.reason_code != request.reason_code
            or (row.reason_text or None) != (request.reason_text or None)
            or row.final_action.get("action") != action
        ):
            raise AppError(
                "review_already_decided",
                409,
                "This review already has a different terminal decision",
            )

    async def _ensure_decision_outbox(
        self, row: Any, payload: dict[str, Any]
    ) -> None:
        if self.topics is None:
            return
        async with self.session_factory() as session:
            await OutboxRepository(session).enqueue_once(
                event_id=f"decision:{row.decision_id}",
                topic=self.topics.analyst_actions,
                event_type="analyst.decision",
                payload=payload,
                trace_id=row.incident_id,
                message_key=row.incident_id,
            )
            await session.commit()

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if self.local_publisher is None:
            return
        try:
            await self.local_publisher("decision_update", payload)
        except Exception as exc:  # noqa: BLE001 - websocket is best effort
            logger.warning("decision_websocket_failed", reason=str(exc)[:500])


def _is_in_flight(status: str) -> bool:
    return status == "resume_pending" or status.startswith("resuming:")


def _lease_expired(status: str) -> bool:
    try:
        return int(status.rsplit(":", 1)[1]) <= int(time.time())
    except (IndexError, ValueError):
        return True


def _new_owner_status() -> str:
    expires_at = int(time.time()) + _RESUME_LEASE_SECONDS
    return f"resuming:{secrets.token_hex(4)}:{expires_at}"


def _review_payload(row: Any) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "reason_text": row.reason_text,
        "actor": row.actor_username,
        "actor_role": row.final_action.get("actor_role"),
        "final_action": row.final_action,
    }


def _decision_id(incident_id: str) -> str:
    digest = hashlib.sha256(f"{incident_id}\0review-v1".encode()).hexdigest()[:20]
    return f"DEC-{digest}"


def decision_dict(row: Any) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "incident_id": row.incident_id,
        "actor_username": row.actor_username,
        "decision": row.decision,
        "status": row.status,
        "reason_code": row.reason_code,
        "reason_text": row.reason_text,
        "original_recommendation": row.original_recommendation,
        "final_action": row.final_action,
        "outcome": row.outcome,
        "decided_at": row.decided_at.isoformat(),
        "outcome_recorded_at": (
            row.outcome_recorded_at.isoformat() if row.outcome_recorded_at else None
        ),
    }
