"""Coordinate deterministic evidence, checkpointed agents, tracing, and audit persistence."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter
from typing import Any

import structlog
from langgraph.types import Command
from langsmith import Client, tracing_context

from backend.app.agents.state import InvestigationState
from backend.app.agents.tools import (
    get_cost_estimate,
    get_historical_baseline,
    get_incident_memory,
    get_segment_stats,
    get_similar_incidents,
    get_window_stats,
)
from backend.app.cache.keys import hash_evidence
from backend.app.config import Settings, get_settings
from backend.app.db.repositories import (
    AuditRepository,
    IncidentRepository,
    LearningRepository,
)
from backend.app.db.session import SessionFactory
from backend.app.monitoring.prometheus import INVESTIGATION_LATENCY, INVESTIGATIONS

logger = structlog.get_logger(__name__)
LocalPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


class InvestigationService:
    def __init__(
        self,
        graph: Any,
        settings: Settings | None = None,
        *,
        session_factory: Any = SessionFactory,
        checkpoint_durable: bool = True,
        local_publisher: LocalPublisher | None = None,
    ) -> None:
        self.graph = graph
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.checkpoint_durable = checkpoint_durable
        self.local_publisher = local_publisher
        self._tasks: set[asyncio.Task[None]] = set()

    async def prepare_state(self, incident_id: str) -> InvestigationState:
        async with self.session_factory() as session:
            repository = IncidentRepository(session)
            incident = await repository.get(incident_id)
            if incident is None:
                raise ValueError(f"Unknown incident: {incident_id}")
            transactions = await repository.window_transactions(incident_id)
            similar = await repository.similar_incidents(incident_id)
            amount_mean = sum(float(row.amount_inr) for row in transactions) / max(
                len(transactions), 1
            )
            memory_attributes = {
                "conditions": (
                    list(incident.segments[0].conditions) if incident.segments else []
                ),
                "scenario_signature": incident.reason,
                "amount_band": (
                    "low" if amount_mean < 1_000 else "medium" if amount_mean < 10_000 else "high"
                ),
            }
            memories = await LearningRepository(session).similar_memories(
                incident_id, memory_attributes
            )
            impact, impact_evidence = get_cost_estimate(
                incident, transactions, self.settings
            )
            records = [
                get_window_stats(incident, transactions),
                *get_segment_stats(incident),
                get_historical_baseline(incident),
                get_similar_incidents(incident, similar),
                get_incident_memory(incident, memories),
                impact_evidence,
            ]
            await repository.save_evidence(
                incident_id, [record.model_dump(mode="json") for record in records]
            )
            await repository.set_status(incident_id, "investigating")
            await session.commit()
            prepared = InvestigationState(
                incident_id=incident.incident_id,
                trace_id=incident.trace_id or incident.incident_id,
                detector_output=dict(incident.detector_output),
                persisted_segments=[
                    {
                        "rank": segment.rank,
                        "conditions": list(segment.conditions),
                        "support": segment.support,
                        "density_lift": segment.density_lift,
                    }
                    for segment in incident.segments
                ],
                evidence=[record.model_dump(mode="json") for record in records],
                impact=impact.model_dump(mode="json"),
                provenance=[],
                degraded=not self.checkpoint_durable,
                status="prepared",
            )
        if self.local_publisher is not None:
            await self.local_publisher(
                "incident_update",
                {"incident_id": incident_id, "status": "investigating"},
            )
        return prepared

    async def investigate(self, incident_id: str) -> InvestigationState:
        started = perf_counter()
        state = await self.prepare_state(incident_id)
        try:
            result = await self._invoke(state, incident_id)
            await self._persist(result)
        except Exception:
            INVESTIGATIONS.labels(status="failed").inc()
            INVESTIGATION_LATENCY.observe(perf_counter() - started)
            raise
        INVESTIGATIONS.labels(
            status="degraded" if result.get("degraded") else "completed"
        ).inc()
        INVESTIGATION_LATENCY.observe(perf_counter() - started)
        return _public_state(result)

    async def resume(
        self, incident_id: str, review: Mapping[str, Any]
    ) -> InvestigationState:
        async with self.session_factory() as session:
            incident = await IncidentRepository(session).get(incident_id)
            if incident is None:
                raise ValueError(f"Unknown incident: {incident_id}")
            trace_id = incident.trace_id or incident_id
        result = await self._invoke(
            Command(resume=dict(review)), incident_id, trace_id=trace_id
        )
        await self._persist(result)
        return _public_state(result)

    async def get_state(self, incident_id: str) -> InvestigationState:
        snapshot = await self.graph.aget_state(
            {"configurable": {"thread_id": incident_id}}
        )
        if not snapshot.values:
            raise ValueError(f"Investigation has not started: {incident_id}")
        return _public_state(InvestigationState(**dict(snapshot.values)))

    async def reconcile(self, state: InvestigationState) -> InvestigationState:
        """Idempotently repair ORM projections from an advanced durable checkpoint."""

        await self._persist(state)
        return _public_state(state)

    async def continue_from_checkpoint(self, incident_id: str) -> InvestigationState:
        """Continue work after an accepted review without injecting it a second time."""

        async with self.session_factory() as session:
            incident = await IncidentRepository(session).get(incident_id)
            if incident is None:
                raise ValueError(f"Unknown incident: {incident_id}")
            trace_id = incident.trace_id or incident_id
        result = await self._invoke(None, incident_id, trace_id=trace_id)
        await self._persist(result)
        return _public_state(result)

    async def _invoke(
        self,
        value: InvestigationState | Command | None,
        incident_id: str,
        *,
        trace_id: str | None = None,
    ) -> InvestigationState:
        trace_id = trace_id or (
            value.get("trace_id", incident_id)
            if isinstance(value, dict)
            else incident_id
        )
        enabled = bool(self.settings.langsmith_tracing and self.settings.langsmith_api_key)
        client = (
            Client(api_key=self.settings.langsmith_api_key) if enabled else None
        )
        config = {
            "configurable": {"thread_id": incident_id},
            "metadata": {"incident_id": incident_id, "trace_id": trace_id},
            "tags": [self.settings.app_env, "fraud-investigation"],
        }
        with tracing_context(
            project_name=self.settings.langsmith_project,
            metadata={"incident_id": incident_id, "trace_id": trace_id},
            enabled=enabled,
            client=client,
        ):
            return await self.graph.ainvoke(value, config=config)

    async def _persist(self, state: InvestigationState) -> None:
        incident_id = state["incident_id"]
        evidence_hash = hash_evidence(state.get("evidence", []))
        provenance = state.get("provenance", [])
        stages = {
            "spike_analysis": "lead_spike_analysis",
            "segment": "segment_interpretation",
            "hypotheses": "root_cause_hypotheses",
            "verification": "evidence_verification",
            "impact": "deterministic_impact",
            "responses": "response_recommendations",
            "response_policy": "response_policy_shadow",
            "synthesis": "lead_synthesis",
            "alert_explanation": "alert_explanation",
        }
        async with self.session_factory() as session:
            repository = IncidentRepository(session)
            for field, agent_name in stages.items():
                if field not in state:
                    continue
                stage_provenance = _stage_provenance(field, provenance)
                call_degraded = bool(stage_provenance.get("degraded"))
                output = state[field]
                output_degraded = bool(
                    call_degraded
                    or (isinstance(output, dict) and output.get("degraded", False))
                )
                stage_provenance = {
                    **stage_provenance,
                    "provider_degraded": call_degraded,
                    "degraded": output_degraded,
                }
                prompt_version = str(stage_provenance.get("prompt_version", "unknown"))
                digest = hashlib.sha256(
                    f"{incident_id}\0{agent_name}\0{prompt_version}".encode()
                ).hexdigest()[:16]
                await repository.save_agent_output(
                    output_id=f"OUT-{digest}",
                    incident_id=incident_id,
                    agent_name=agent_name,
                    status="degraded" if stage_provenance.get("degraded") else "completed",
                    model_name=str(stage_provenance.get("model_name", "deterministic-python")),
                    prompt_version=prompt_version,
                    evidence_hash=evidence_hash,
                    payload={
                        "result": state[field],
                        "provenance": stage_provenance,
                    },
                )
            if "policy_gate" in state:
                gate = state["policy_gate"]
                await AuditRepository(session).append_once(
                    incident_id=incident_id,
                    event_type="policy_evaluated",
                    actor="system",
                    payload={
                        "policy_gate": gate,
                        "policy_context": state.get("policy_context", {}),
                        "policy_basis": state.get("policy_basis", {}),
                        "grounding": state.get("grounded_claims", {}),
                    },
                    trace_id=state.get("trace_id"),
                    idempotency_key=(
                        f"policy:{gate.get('policy_version', 'unknown')}:"
                        f"{gate.get('rule_id', 'unknown')}"
                    ),
                )
            status = state.get("status", "investigating")
            await repository.set_status(incident_id, status)
            await session.commit()
        if self.local_publisher is not None:
            await self.local_publisher(
                "incident_update",
                {"incident_id": incident_id, "status": status},
            )

    async def schedule(self, incident_id: str, _: str) -> None:
        """Start once after incident commit without blocking transaction ingestion."""

        if not self.settings.investigation_auto_start:
            return
        task = asyncio.create_task(
            self._run_safely(incident_id), name=f"investigation-{incident_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_safely(self, incident_id: str) -> None:
        try:
            await self.investigate(incident_id)
        except Exception:
            logger.exception("investigation_failed", incident_id=incident_id)
            async with self.session_factory() as session:
                await IncidentRepository(session).set_status(
                    incident_id, "investigation_failed"
                )
                await session.commit()
            if self.local_publisher is not None:
                await self.local_publisher(
                    "incident_update",
                    {"incident_id": incident_id, "status": "investigation_failed"},
                )

    async def close(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def _stage_provenance(field: str, provenance: list[dict[str, Any]]) -> dict[str, Any]:
    node_map = {
        "spike_analysis": "analyze_spike",
        "segment": "discover_segment",
        "hypotheses": "investigate_root_cause",
        "verification": "verify_evidence",
        "responses": "evaluate_responses",
        "response_policy": "evaluate_responses",
        "synthesis": "finalize",
        "alert_explanation": "finalize",
    }
    if field == "impact":
        return {
            "model_name": "deterministic-python",
            "prompt_version": "deterministic-impact-v1",
            "degraded": False,
        }
    target = node_map[field]
    output = {
        "synthesis": "synthesis",
        "alert_explanation": "alert",
    }.get(field)
    matches = [
        item
        for item in provenance
        if item.get("node") == target
        and (output is None or item.get("output") == output)
    ]
    return matches[-1] if matches else {
        "model_name": "unknown",
        "prompt_version": "unknown",
        "degraded": True,
    }


def _public_state(state: InvestigationState) -> InvestigationState:
    return InvestigationState(
        **{key: value for key, value in state.items() if not key.startswith("__")}
    )
