"""Thin async repositories for transactions, incidents, and analyst feedback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.db.models import (
    AgentOutput,
    AnalystDecision,
    AuditEvent,
    Evidence,
    FraudScore,
    Incident,
    IncidentMemory,
    IncidentSegment,
    ModelVersion,
    OutboxEvent,
    Policy,
    PolicyVersion,
    Reward,
    Transaction,
)
from backend.app.db.models.base import utc_now


class SerializableRecord(Protocol):
    def to_dict(self) -> dict[str, object]: ...


Record = Mapping[str, Any] | SerializableRecord


@dataclass(frozen=True)
class TimelineEntry:
    timestamp: datetime
    kind: str
    payload: dict[str, Any]


def _record_dict(record: Record) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    return dict(record.to_dict())


def _naive_utc(value: object) -> datetime:
    if isinstance(value, str):
        result = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        result = value
    elif hasattr(value, "to_pydatetime"):
        result = value.to_pydatetime()
    else:
        raise TypeError(f"Expected a datetime-compatible value, got {type(value).__name__}")
    if result.tzinfo is not None:
        result = result.astimezone(UTC).replace(tzinfo=None)
    return result


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, transaction_id: str) -> Transaction | None:
        result = await self.session.execute(
            select(Transaction)
            .options(selectinload(Transaction.score))
            .where(Transaction.transaction_id == transaction_id)
        )
        return result.scalar_one_or_none()

    async def insert_with_score(
        self,
        transaction: Mapping[str, Any],
        score: Mapping[str, Any],
        *,
        model_version_id: str | None = None,
    ) -> tuple[Transaction, FraudScore, bool]:
        """Flush one transaction and score atomically; duplicates return the stored pair."""

        transaction_id = str(transaction["transaction_id"])
        existing = await self.get(transaction_id)
        if existing is not None and existing.score is not None:
            return existing, existing.score, False

        transaction_values = dict(transaction)
        transaction_values["timestamp"] = _naive_utc(transaction_values["timestamp"])
        if transaction_values.get("ingested_at") is not None:
            transaction_values["ingested_at"] = _naive_utc(transaction_values["ingested_at"])
        score_values = {
            "risk_probability": float(score["risk_probability"]),
            "decision_score": float(score["decision_score"]),
            "decision_threshold": float(score["decision_threshold"]),
            "score_space": str(score["score_space"]),
            "degraded": bool(score.get("degraded", False)),
            "reason": score.get("reason"),
            "scored_at": _naive_utc(score["scored_at"]) if score.get("scored_at") else utc_now(),
            "model_version_id": model_version_id,
        }
        try:
            async with self.session.begin_nested():
                if existing is None:
                    existing = Transaction(**transaction_values)
                    self.session.add(existing)
                    await self.session.flush()
                score_row = FraudScore(transaction_id=transaction_id, **score_values)
                self.session.add(score_row)
                await self.session.flush()
        except IntegrityError:
            stored = await self.get(transaction_id)
            if stored is None or stored.score is None:
                raise
            return stored, stored.score, False

        assert existing is not None
        existing.score = score_row
        return existing, score_row, True

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(Transaction)) or 0)

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Transaction]:
        statement = select(Transaction).options(selectinload(Transaction.score))
        if start is not None:
            statement = statement.where(Transaction.timestamp >= _naive_utc(start))
        if end is not None:
            statement = statement.where(Transaction.timestamp < _naive_utc(end))
        statement = statement.order_by(Transaction.timestamp.desc()).limit(limit).offset(offset)
        return list((await self.session.scalars(statement)).all())

    async def register_model_version(
        self,
        values: Mapping[str, Any],
    ) -> tuple[ModelVersion, bool]:
        model_version_id = str(values["model_version_id"])
        model_values = dict(values)
        for field in ("registered_at", "activated_at"):
            if model_values.get(field) is not None:
                model_values[field] = _naive_utc(model_values[field])
        existing = await self.session.get(ModelVersion, model_version_id)
        if existing is not None:
            return existing, False
        try:
            async with self.session.begin_nested():
                model = ModelVersion(**model_values)
                self.session.add(model)
                await self.session.flush()
        except IntegrityError:
            statement = select(ModelVersion).where(
                ModelVersion.name == str(values["name"]),
                ModelVersion.version == str(values["version"]),
            )
            existing = (await self.session.scalars(statement)).one()
            return existing, False
        return model, True

    async def list_model_versions(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ModelVersion]:
        statement = select(ModelVersion)
        if status is not None:
            statement = statement.where(ModelVersion.status == status)
        statement = statement.order_by(ModelVersion.registered_at.desc()).limit(limit)
        return list((await self.session.scalars(statement)).all())


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, incident_id: str) -> Incident | None:
        statement = (
            select(Incident)
            .options(selectinload(Incident.segments))
            .where(Incident.incident_id == incident_id)
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def _get_by_alert(self, alert_id: str) -> Incident | None:
        statement = (
            select(Incident)
            .options(selectinload(Incident.segments))
            .where(Incident.alert_id == alert_id)
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def create(
        self,
        *,
        incident_id: str,
        alert: Record,
        segments: Sequence[Record] = (),
        exposure_estimate_inr: float = 0.0,
        trace_id: str | None = None,
        status: str = "detected",
    ) -> tuple[Incident, bool]:
        """Create an alert-backed incident once and persist ranked segment findings."""

        alert_values = _record_dict(alert)
        alert_id = str(alert_values["alert_id"])
        existing = await self._get_by_alert(alert_id)
        if existing is not None:
            return existing, False

        incident = Incident(
            incident_id=incident_id,
            alert_id=alert_id,
            status=status,
            detected_at=_naive_utc(alert_values["fire_timestamp"]),
            window_start=_naive_utc(alert_values["window_start"]),
            window_end=_naive_utc(alert_values["fire_timestamp"]),
            reason=str(alert_values["reason"]),
            detector_output=alert_values,
            exposure_estimate_inr=float(exposure_estimate_inr),
            trace_id=trace_id,
        )
        for rank, segment in enumerate(segments, start=1):
            values = _record_dict(segment)
            incident.segments.append(
                IncidentSegment(
                    rank=rank,
                    conditions=values["conditions"],
                    support=int(values["support"]),
                    baseline_support=int(values["baseline_support"]),
                    risk_density=float(values["risk_density"]),
                    baseline_risk_density=float(values["baseline_risk_density"]),
                    density_lift=float(values["density_lift"]),
                    prevalence_lift=float(values["prevalence_lift"]),
                    excess_risk_contribution=float(values["excess_risk_contribution"]),
                    p_value=float(values["p_value"]),
                    rank_score=float(values["score"]),
                    condition_contributions=values.get("condition_contributions", []),
                )
            )
        try:
            async with self.session.begin_nested():
                self.session.add(incident)
                await self.session.flush()
        except IntegrityError:
            existing = await self._get_by_alert(alert_id)
            if existing is None:
                raise
            return existing, False
        return incident, True

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Incident]:
        statement = select(Incident).options(selectinload(Incident.segments))
        if status is not None:
            statement = statement.where(Incident.status == status)
        statement = statement.order_by(Incident.detected_at.desc()).limit(limit).offset(offset)
        return list((await self.session.scalars(statement)).unique().all())

    async def count(self, *, status: str | None = None) -> int:
        statement = select(func.count()).select_from(Incident)
        if status is not None:
            statement = statement.where(Incident.status == status)
        return int(await self.session.scalar(statement) or 0)

    async def timeline(self, incident_id: str) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []
        evidence = await self.session.scalars(
            select(Evidence).where(Evidence.incident_id == incident_id)
        )
        entries.extend(
            TimelineEntry(
                row.created_at,
                "evidence",
                {
                    "evidence_id": row.evidence_id,
                    "evidence_type": row.evidence_type,
                    "source": row.source,
                    "strength": row.strength,
                    "payload": row.payload,
                },
            )
            for row in evidence
        )
        outputs = await self.session.scalars(
            select(AgentOutput).where(AgentOutput.incident_id == incident_id)
        )
        entries.extend(
            TimelineEntry(
                row.created_at,
                "agent_output",
                {
                    "output_id": row.output_id,
                    "agent_name": row.agent_name,
                    "status": row.status,
                    "payload": row.payload,
                },
            )
            for row in outputs
        )
        decisions = await self.session.scalars(
            select(AnalystDecision).where(AnalystDecision.incident_id == incident_id)
        )
        entries.extend(
            TimelineEntry(
                row.decided_at,
                "analyst_decision",
                {
                    "decision_id": row.decision_id,
                    "actor_username": row.actor_username,
                    "decision": row.decision,
                    "status": row.status,
                    "reason_code": row.reason_code,
                    "reason_text": row.reason_text,
                    "final_action": row.final_action,
                    "outcome": row.outcome,
                },
            )
            for row in decisions
        )
        audit_events = await self.session.scalars(
            select(AuditEvent).where(AuditEvent.incident_id == incident_id)
        )
        entries.extend(
            TimelineEntry(
                row.timestamp,
                "audit_event",
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "actor": row.actor,
                    "payload": row.payload,
                    "trace_id": row.trace_id,
                },
            )
            for row in audit_events
        )
        return sorted(entries, key=lambda entry: entry.timestamp)

    async def get_with_timeline(
        self,
        incident_id: str,
    ) -> tuple[Incident | None, list[TimelineEntry]]:
        incident = await self.get(incident_id)
        if incident is None:
            return None, []
        return incident, await self.timeline(incident_id)

    async def list_evidence(self, incident_id: str) -> list[Evidence]:
        statement = (
            select(Evidence)
            .where(Evidence.incident_id == incident_id)
            .order_by(Evidence.created_at, Evidence.evidence_id)
        )
        return list((await self.session.scalars(statement)).all())

    async def save_evidence(
        self, incident_id: str, records: Sequence[Mapping[str, Any]]
    ) -> list[Evidence]:
        """Persist deterministic evidence IDs idempotently."""

        if not records:
            return await self.list_evidence(incident_id)
        ids = [str(record["evidence_id"]) for record in records]
        existing = set(
            await self.session.scalars(
                select(Evidence.evidence_id).where(Evidence.evidence_id.in_(ids))
            )
        )
        for record in records:
            evidence_id = str(record["evidence_id"])
            if evidence_id in existing:
                continue
            self.session.add(
                Evidence(
                    evidence_id=evidence_id,
                    incident_id=incident_id,
                    evidence_type=str(record["evidence_type"]),
                    source=str(record["source"]),
                    payload=dict(record["payload"]),
                    strength=str(record["strength"]),
                )
            )
        await self.session.flush()
        return await self.list_evidence(incident_id)

    async def save_agent_output(
        self,
        *,
        output_id: str,
        incident_id: str,
        agent_name: str,
        status: str,
        payload: Mapping[str, Any],
        model_name: str | None = None,
        prompt_version: str | None = None,
        evidence_hash: str | None = None,
    ) -> AgentOutput:
        """Upsert a deterministic stage output so graph replay cannot duplicate audit rows."""

        row = await self.session.get(AgentOutput, output_id)
        if row is None:
            row = AgentOutput(
                output_id=output_id,
                incident_id=incident_id,
                agent_name=agent_name,
                status=status,
                model_name=model_name,
                prompt_version=prompt_version,
                evidence_hash=evidence_hash,
                payload=dict(payload),
            )
            self.session.add(row)
        else:
            row.status = status
            row.model_name = model_name
            row.prompt_version = prompt_version
            row.evidence_hash = evidence_hash
            row.payload = dict(payload)
        await self.session.flush()
        return row

    async def list_agent_outputs(self, incident_id: str) -> list[AgentOutput]:
        statement = (
            select(AgentOutput)
            .where(AgentOutput.incident_id == incident_id)
            .order_by(AgentOutput.created_at, AgentOutput.output_id)
        )
        return list((await self.session.scalars(statement)).all())

    async def window_transactions(self, incident_id: str) -> list[Transaction]:
        incident = await self.get(incident_id)
        if incident is None:
            return []
        statement = (
            select(Transaction)
            .options(selectinload(Transaction.score))
            .where(
                Transaction.timestamp > incident.window_start,
                Transaction.timestamp <= incident.window_end,
            )
            .order_by(Transaction.timestamp)
        )
        return list((await self.session.scalars(statement)).all())

    async def similar_incidents(self, incident_id: str, limit: int = 5) -> list[Incident]:
        incident = await self.get(incident_id)
        if incident is None:
            return []
        statement = (
            select(Incident)
            .where(
                Incident.incident_id != incident_id,
                Incident.detected_at < incident.detected_at,
            )
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(statement)).all())

    async def set_status(self, incident_id: str, status: str) -> Incident | None:
        incident = await self.get(incident_id)
        if incident is None:
            return None
        incident.status = status
        incident.updated_at = utc_now()
        await self.session.flush()
        return incident


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_analyst_feedback(
        self,
        *,
        decision_id: str,
        incident_id: str,
        actor_username: str,
        decision: str,
        reason_code: str,
        original_recommendation: Mapping[str, Any],
        final_action: Mapping[str, Any],
        reason_text: str | None = None,
        status: str = "recorded",
        decided_at: datetime | None = None,
    ) -> tuple[AnalystDecision, bool]:
        existing = await self.session.get(AnalystDecision, decision_id)
        if existing is not None:
            return existing, False
        row = AnalystDecision(
            decision_id=decision_id,
            incident_id=incident_id,
            actor_username=actor_username,
            decision=decision,
            status=status,
            reason_code=reason_code,
            reason_text=reason_text,
            original_recommendation=dict(original_recommendation),
            final_action=dict(final_action),
            decided_at=_naive_utc(decided_at) if decided_at is not None else utc_now(),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.session.get(AnalystDecision, decision_id)
            if existing is None:
                raise
            return existing, False
        return row, True

    async def get(self, decision_id: str) -> AnalystDecision | None:
        return await self.session.get(AnalystDecision, decision_id)

    async def compare_and_set_status(
        self, decision_id: str, expected: str, status: str
    ) -> bool:
        result = await self.session.execute(
            update(AnalystDecision)
            .where(
                AnalystDecision.decision_id == decision_id,
                AnalystDecision.status == expected,
            )
            .values(status=status)
        )
        await self.session.flush()
        return bool(result.rowcount == 1)

    async def set_status(self, decision_id: str, status: str) -> AnalystDecision | None:
        row = await self.get(decision_id)
        if row is None:
            return None
        row.status = status
        await self.session.flush()
        return row

    async def compare_and_set_outcome(
        self,
        decision_id: str,
        outcome: Mapping[str, Any],
        *,
        recorded_at: datetime | None = None,
    ) -> tuple[AnalystDecision | None, bool]:
        timestamp = _naive_utc(recorded_at) if recorded_at is not None else utc_now()
        result = await self.session.execute(
            update(AnalystDecision)
            .where(
                AnalystDecision.decision_id == decision_id,
                AnalystDecision.status == "completed",
                AnalystDecision.outcome.is_(None),
            )
            .values(
                outcome=dict(outcome),
                outcome_recorded_at=timestamp,
                status="outcome_recorded",
            )
        )
        await self.session.flush()
        row = await self.session.get(AnalystDecision, decision_id, populate_existing=True)
        return row, bool(result.rowcount == 1)

    async def list_for_incident(self, incident_id: str) -> list[AnalystDecision]:
        statement = (
            select(AnalystDecision)
            .where(AnalystDecision.incident_id == incident_id)
            .order_by(AnalystDecision.decided_at)
        )
        return list((await self.session.scalars(statement)).all())


class AuditRepository:
    """Append-only audit access; callers supply stable idempotency keys."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append_once(
        self,
        *,
        incident_id: str | None,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
        trace_id: str | None,
        idempotency_key: str,
    ) -> tuple[AuditEvent, bool]:
        rows = await self.session.scalars(
            select(AuditEvent).where(
                AuditEvent.incident_id == incident_id,
                AuditEvent.event_type == event_type,
            )
        )
        for row in rows:
            if row.payload.get("idempotency_key") == idempotency_key:
                return row, False
        row = AuditEvent(
            incident_id=incident_id,
            event_type=event_type,
            actor=actor,
            payload={**payload, "idempotency_key": idempotency_key},
            trace_id=trace_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    async def list_for_incident(self, incident_id: str) -> list[AuditEvent]:
        statement = (
            select(AuditEvent)
            .where(AuditEvent.incident_id == incident_id)
            .order_by(AuditEvent.timestamp, AuditEvent.event_id)
        )
        return list((await self.session.scalars(statement)).all())


def _validate_reward_replay(
    row: Reward,
    *,
    incident_id: str,
    decision_id: str | None,
    action: str,
    total_reward: float,
    components: Mapping[str, Any],
    assumptions_version: str,
    reward_kind: str,
    evaluation_run_id: str | None,
) -> None:
    expected = (
        incident_id,
        decision_id,
        action,
        float(total_reward),
        dict(components),
        assumptions_version,
        reward_kind,
        evaluation_run_id,
    )
    actual = (
        row.incident_id,
        row.decision_id,
        row.action,
        float(row.total_reward),
        row.components,
        row.assumptions_version,
        row.reward_kind,
        row.evaluation_run_id,
    )
    if actual != expected:
        raise ValueError("Reward idempotency key conflicts with immutable reward inputs")


class LearningRepository:
    """Compact persistence boundary for rewards, memory, and response-policy versions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_reward(
        self,
        *,
        incident_id: str,
        action: str,
        total_reward: float,
        components: Mapping[str, Any],
        idempotency_key: str,
        assumptions_version: str,
        decision_id: str | None = None,
        reward_kind: str = "observed",
        evaluation_run_id: str | None = None,
    ) -> tuple[Reward, bool]:
        values = {
            "incident_id": incident_id,
            "decision_id": decision_id,
            "action": action,
            "total_reward": total_reward,
            "components": components,
            "assumptions_version": assumptions_version,
            "reward_kind": reward_kind,
            "evaluation_run_id": evaluation_run_id,
        }
        existing = await self.session.scalar(
            select(Reward).where(Reward.idempotency_key == idempotency_key)
        )
        if existing is not None:
            _validate_reward_replay(existing, **values)
            return existing, False
        row = Reward(
            incident_id=incident_id,
            decision_id=decision_id,
            action=action,
            total_reward=total_reward,
            components=dict(components),
            idempotency_key=idempotency_key,
            assumptions_version=assumptions_version,
            reward_kind=reward_kind,
            evaluation_run_id=evaluation_run_id,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.session.scalar(
                select(Reward).where(Reward.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise
            _validate_reward_replay(existing, **values)
            return existing, False
        return row, True

    async def upsert_memory(
        self,
        *,
        incident_id: str,
        summary: str,
        attributes: Mapping[str, Any],
        outcome_tags: Sequence[Any],
    ) -> IncidentMemory:
        row = await self.session.scalar(
            select(IncidentMemory).where(IncidentMemory.incident_id == incident_id)
        )
        if row is None:
            row = IncidentMemory(
                incident_id=incident_id,
                summary=summary,
                attributes=dict(attributes),
                outcome_tags=list(outcome_tags),
            )
            self.session.add(row)
        else:
            row.summary = summary
            row.attributes = dict(attributes)
            row.outcome_tags = list(outcome_tags)
            row.updated_at = utc_now()
        await self.session.flush()
        return row

    async def similar_memories(
        self, incident_id: str, attributes: Mapping[str, Any], limit: int = 5
    ) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.scalars(
                    select(IncidentMemory).where(
                        IncidentMemory.incident_id != incident_id
                    )
                )
            ).all()
        )
        current_conditions = set(attributes.get("conditions", []))

        def similarity(row: IncidentMemory) -> float:
            other = set(row.attributes.get("conditions", []))
            union = current_conditions | other
            overlap = len(current_conditions & other) / max(len(union), 1)
            signature = float(
                bool(attributes.get("scenario_signature"))
                and attributes.get("scenario_signature")
                == row.attributes.get("scenario_signature")
            )
            amount = float(
                bool(attributes.get("amount_band"))
                and attributes.get("amount_band") == row.attributes.get("amount_band")
            )
            return 0.6 * overlap + 0.25 * signature + 0.15 * amount

        ranked = sorted(
            ((similarity(row), row) for row in rows),
            key=lambda item: (item[0], item[1].updated_at),
            reverse=True,
        )
        return [
            {
                "incident_id": row.incident_id,
                "summary": row.summary,
                "attributes": row.attributes,
                "outcome_tags": row.outcome_tags,
                "similarity": round(score, 6),
            }
            for score, row in ranked[:limit]
            if score > 0
        ]

    async def ensure_policy(
        self, policy_id: str = "response-policy", name: str = "Learned response policy"
    ) -> Policy:
        row = await self.session.get(Policy, policy_id)
        if row is None:
            row = Policy(
                policy_id=policy_id,
                name=name,
                description="Offline-trained response optimization; safety policy remains authoritative.",
                status="shadow",
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def create_policy_version(
        self,
        *,
        version: int,
        rules: Mapping[str, Any],
        metrics: Mapping[str, Any],
        created_by: str,
        status: str = "candidate",
        artifact_uri: str | None = None,
        artifact_checksum: str | None = None,
        parent_version: int | None = None,
        policy_id: str = "response-policy",
    ) -> PolicyVersion:
        await self.ensure_policy(policy_id)
        existing = await self.session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy_id,
                PolicyVersion.version == version,
            )
        )
        if existing is not None:
            return existing
        row = PolicyVersion(
            policy_id=policy_id,
            version=version,
            status=status,
            rules=dict(rules),
            artifact_uri=artifact_uri,
            artifact_checksum=artifact_checksum,
            metrics=dict(metrics),
            parent_version=parent_version,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_policy_versions(
        self, policy_id: str = "response-policy"
    ) -> list[PolicyVersion]:
        return list(
            (
                await self.session.scalars(
                    select(PolicyVersion)
                    .where(PolicyVersion.policy_id == policy_id)
                    .order_by(PolicyVersion.version.desc())
                )
            ).all()
        )

    async def get_policy_version(self, policy_version_id: int) -> PolicyVersion | None:
        return await self.session.get(PolicyVersion, policy_version_id)

    async def active_policy_version(
        self, policy_id: str = "response-policy"
    ) -> PolicyVersion | None:
        policy = await self.session.get(Policy, policy_id)
        if policy is None or policy.active_version is None:
            return None
        return await self.session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy_id,
                PolicyVersion.version == policy.active_version,
            )
        )

    async def activate_version(
        self,
        policy_version_id: int,
        actor: str,
        *,
        expected_active_policy_version_id: int | None = None,
    ) -> tuple[PolicyVersion, PolicyVersion | None]:
        target = await self.session.get(PolicyVersion, policy_version_id)
        if target is None:
            raise ValueError("Unknown response policy version")
        policy = await self.session.scalar(
            select(Policy).where(Policy.policy_id == target.policy_id).with_for_update()
        )
        if policy is None:
            raise ValueError("Response policy does not exist")
        previous = await self.active_policy_version(target.policy_id)
        if expected_active_policy_version_id is not None and (
            previous is None
            or previous.policy_version_id != expected_active_policy_version_id
        ):
            raise ValueError("Active response policy changed after evaluation")
        if previous is not None and previous.policy_version_id != target.policy_version_id:
            previous.status = "retired"
        target.status = "production"
        target.approved_by = actor
        target.activated_at = utc_now()
        policy.active_version = target.version
        policy.status = "production"
        policy.updated_at = utc_now()
        await self.session.flush()
        return target, previous


class OutboxRepository:
    """Portable transactional outbox with conditional claim leases."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue_once(
        self,
        *,
        event_id: str,
        topic: str,
        event_type: str,
        payload: Mapping[str, Any],
        trace_id: str,
        message_key: str | None,
        occurred_at: datetime | None = None,
    ) -> tuple[OutboxEvent, bool]:
        existing = await self.session.get(OutboxEvent, event_id)
        values = (topic, event_type, dict(payload), trace_id, message_key)
        if existing is not None:
            if (
                existing.topic,
                existing.event_type,
                existing.payload,
                existing.trace_id,
                existing.message_key,
            ) != values:
                raise ValueError("Outbox event ID conflicts with immutable event content")
            return existing, False
        timestamp = _naive_utc(occurred_at) if occurred_at is not None else utc_now()
        row = OutboxEvent(
            event_id=event_id,
            topic=topic,
            event_type=event_type,
            payload=dict(payload),
            trace_id=trace_id,
            message_key=message_key,
            occurred_at=timestamp,
            available_at=timestamp,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.session.get(OutboxEvent, event_id)
            if existing is None:
                raise
            if (
                existing.topic,
                existing.event_type,
                existing.payload,
                existing.trace_id,
                existing.message_key,
            ) != values:
                raise ValueError("Outbox event ID conflicts with immutable event content")
            return existing, False
        return row, True

    async def claim_batch(
        self, *, worker_id: str, limit: int, lease_seconds: int
    ) -> list[OutboxEvent]:
        now = utc_now()
        claimable = and_(
            OutboxEvent.published_at.is_(None),
            OutboxEvent.available_at <= now,
            or_(
                OutboxEvent.status == "pending",
                and_(
                    OutboxEvent.status == "publishing",
                    OutboxEvent.claim_until < now,
                ),
            ),
        )
        ids = list(
            (
                await self.session.scalars(
                    select(OutboxEvent.event_id)
                    .where(claimable)
                    .order_by(OutboxEvent.created_at, OutboxEvent.event_id)
                    .limit(limit)
                )
            ).all()
        )
        lease_until = now + timedelta(seconds=lease_seconds)
        claimed: list[str] = []
        for event_id in ids:
            result = await self.session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.event_id == event_id, claimable)
                .values(
                    status="publishing",
                    claimed_by=worker_id,
                    claim_until=lease_until,
                    attempts=OutboxEvent.attempts + 1,
                )
            )
            if result.rowcount == 1:
                claimed.append(event_id)
        await self.session.flush()
        if not claimed:
            return []
        return list(
            (
                await self.session.scalars(
                    select(OutboxEvent).where(OutboxEvent.event_id.in_(claimed))
                )
            ).all()
        )

    async def mark_published(self, event_id: str, worker_id: str) -> bool:
        result = await self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.event_id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.claimed_by == worker_id,
            )
            .values(
                status="published",
                published_at=utc_now(),
                claimed_by=None,
                claim_until=None,
                last_error=None,
            )
        )
        return bool(result.rowcount == 1)

    async def mark_failed(
        self, event_id: str, worker_id: str, error: str, attempts: int
    ) -> bool:
        result = await self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.event_id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.claimed_by == worker_id,
            )
            .values(
                status="pending",
                available_at=utc_now() + timedelta(seconds=min(60, 2 ** min(attempts, 5))),
                claimed_by=None,
                claim_until=None,
                last_error=error[:500],
            )
        )
        return bool(result.rowcount == 1)
