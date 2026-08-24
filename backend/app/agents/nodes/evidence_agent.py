"""Generate evidence-linked candidate root-cause hypotheses."""

from __future__ import annotations

from pathlib import Path

from backend.app.agents.state import (
    HypothesisSet,
    InvestigationState,
    RootCauseHypothesis,
)
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier

PROMPT_VERSION = "evidence-v1"
PROMPT = Path(__file__).parents[1].joinpath("prompts/evidence_v1.txt").read_text(encoding="utf-8")


class EvidenceAgent:
    def __init__(self, gateway: StructuredLLMGateway) -> None:
        self.gateway = gateway

    async def __call__(self, state: InvestigationState) -> dict[str, object]:
        evidence = state["evidence"]
        window_ids = [
            item["evidence_id"]
            for item in evidence
            if item["evidence_type"] in {"window_statistics", "historical_baseline"}
        ]
        segment_ids = [
            item["evidence_id"]
            for item in evidence
            if item["evidence_type"] == "segment_statistics"
        ]
        all_ids = [str(item["evidence_id"]) for item in evidence]

        def fallback() -> HypothesisSet:
            return HypothesisSet(
                hypotheses=[
                    RootCauseHypothesis(
                        claim_id=f"CLM-{state['incident_id']}-01",
                        statement="Calibrated transaction risk density increased materially above its historical baseline.",
                        evidence_ids=window_ids or all_ids[:1],
                        strength="strong",
                    ),
                    RootCauseHypothesis(
                        claim_id=f"CLM-{state['incident_id']}-02",
                        statement="The leading deterministic cohort contributes to the elevated-risk activity and warrants focused review.",
                        evidence_ids=segment_ids or all_ids[:1],
                        strength="moderate",
                    ),
                ]
            )

        result = await self.gateway.generate(
            ModelTier.PRIMARY,
            PROMPT,
            {
                "segment": state["segment"],
                "spike_analysis": state["spike_analysis"],
                "evidence_store": evidence,
            },
            HypothesisSet,
            fallback,
            prompt_version=PROMPT_VERSION,
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        provenance = list(state.get("provenance", []))
        provenance.append(
            {
                "node": "investigate_root_cause",
                "prompt_version": PROMPT_VERSION,
                **result.provenance(),
            }
        )
        return {
            "hypotheses": [item.model_dump(mode="json") for item in result.output.hypotheses],
            "provenance": provenance,
            "degraded": bool(state.get("degraded", False) or result.degraded),
        }
