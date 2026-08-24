"""Interpret, but never compute, deterministic segment findings."""

from __future__ import annotations

from pathlib import Path

from backend.app.agents.state import InvestigationState, SegmentInterpretation
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier

PROMPT_VERSION = "segment-v1"
PROMPT = Path(__file__).parents[1].joinpath("prompts/segment_v1.txt").read_text(encoding="utf-8")


class SegmentAgent:
    def __init__(self, gateway: StructuredLLMGateway) -> None:
        self.gateway = gateway

    async def __call__(self, state: InvestigationState) -> dict[str, object]:
        evidence = [
            item for item in state["evidence"] if item["evidence_type"] == "segment_statistics"
        ]
        first = evidence[0]
        payload = first["payload"]

        def fallback() -> SegmentInterpretation:
            return SegmentInterpretation(
                name=str(payload.get("name", "elevated-risk transaction window")),
                description="Deterministic risk-density evidence identifies this cohort as the leading available segment.",
                conditions=list(payload.get("conditions", [])),
                evidence_ids=[str(first["evidence_id"])],
            )

        result = await self.gateway.generate(
            ModelTier.SECONDARY,
            PROMPT,
            {"segment_evidence": evidence},
            SegmentInterpretation,
            fallback,
            prompt_version=PROMPT_VERSION,
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        authoritative = SegmentInterpretation(
            name=str(payload.get("name", "elevated-risk transaction window")),
            description=result.output.description,
            conditions=list(payload.get("conditions", [])),
            evidence_ids=[str(first["evidence_id"])],
        )
        provenance = list(state.get("provenance", []))
        provenance.append(
            {"node": "discover_segment", "prompt_version": PROMPT_VERSION, **result.provenance()}
        )
        return {
            "segment": authoritative.model_dump(mode="json"),
            "provenance": provenance,
            "degraded": bool(state.get("degraded", False) or result.degraded),
        }
