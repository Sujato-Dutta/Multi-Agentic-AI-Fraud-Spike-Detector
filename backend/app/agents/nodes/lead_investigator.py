"""Lead analysis, synthesis, and economy-tier analyst summary."""

from __future__ import annotations

from pathlib import Path

from backend.app.agents.state import (
    AlertExplanation,
    InvestigationState,
    LeadSynthesis,
    SpikeAnalysis,
)
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier

PROMPT_VERSION = "lead-v1"
PROMPT = Path(__file__).parents[1].joinpath("prompts/lead_v1.txt").read_text(encoding="utf-8")


class LeadInvestigator:
    def __init__(self, gateway: StructuredLLMGateway) -> None:
        self.gateway = gateway

    async def analyze(self, state: InvestigationState) -> dict[str, object]:
        evidence = [
            item
            for item in state["evidence"]
            if item["evidence_type"] in {"window_statistics", "historical_baseline"}
        ]
        ids = [str(item["evidence_id"]) for item in evidence]

        def fallback() -> SpikeAnalysis:
            detector = state["detector_output"]
            return SpikeAnalysis(
                summary="The calibrated risk-density detector identified activity materially above its adaptive baseline.",
                anomalies=[
                    f"Risk-density lift: {float(detector.get('density_lift', 0.0)):.2f}x",
                    f"High-risk transactions: {int(detector.get('high_risk_count', 0))}",
                ],
                evidence_ids=ids,
            )

        result = await self.gateway.generate(
            ModelTier.PRIMARY,
            PROMPT,
            {"detector_output": state["detector_output"], "evidence": evidence},
            SpikeAnalysis,
            fallback,
            prompt_version=PROMPT_VERSION,
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        valid_ids = set(ids)
        analysis = result.output.model_copy(
            update={
                "evidence_ids": [
                    item for item in result.output.evidence_ids if item in valid_ids
                ]
                or ids
            }
        )
        provenance = list(state.get("provenance", []))
        provenance.append(
            {"node": "analyze_spike", "prompt_version": PROMPT_VERSION, **result.provenance()}
        )
        return {
            "spike_analysis": analysis.model_dump(mode="json"),
            "provenance": provenance,
            "degraded": bool(state.get("degraded", False) or result.degraded),
        }

    async def finalize(self, state: InvestigationState) -> dict[str, object]:
        evidence_ids = [str(item["evidence_id"]) for item in state["evidence"]]
        available = set(evidence_ids)
        degraded = bool(state.get("degraded", False))

        def synthesis_fallback() -> LeadSynthesis:
            return LeadSynthesis(
                summary="An evidence-backed fraud-risk spike requires human review. Automated narrative capability is degraded; deterministic detector, verification, and impact results remain available.",
                confidence="low" if degraded else "medium",
                escalation_posture="escalate",
                evidence_ids=evidence_ids,
                degraded=True,
            )

        supported_claim_ids = {
            str(item["claim_id"])
            for item in state["verification"]["verdicts"]
            if item["verdict"] == "supported"
        }
        verified_hypotheses = [
            item
            for item in state["hypotheses"]
            if str(item["claim_id"]) in supported_claim_ids
        ]
        synthesis = await self.gateway.generate(
            ModelTier.PRIMARY,
            PROMPT,
            {
                "segment": state["segment"],
                "hypotheses": verified_hypotheses,
                "verification": state["verification"],
                "responses": state["responses"],
                "human_review": state["human_review"],
            },
            LeadSynthesis,
            synthesis_fallback,
            prompt_version=f"{PROMPT_VERSION}-final",
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        synthesis_degraded = bool(degraded or synthesis.degraded or synthesis.output.degraded)
        synthesis_output = synthesis.output.model_copy(
            update={
                "confidence": "low" if synthesis_degraded else synthesis.output.confidence,
                "evidence_ids": [
                    item for item in synthesis.output.evidence_ids if item in available
                ]
                or evidence_ids,
                "degraded": synthesis_degraded,
            }
        )

        def explanation_fallback() -> AlertExplanation:
            return AlertExplanation(
                title="Evidence-backed fraud spike requires review",
                analyst_summary=synthesis_output.summary,
                next_step="Review the verified evidence and ranked responses; no action has been authorized.",
            )

        explanation = await self.gateway.generate(
            ModelTier.ECONOMY,
            PROMPT,
            {
                "synthesis": synthesis_output.model_dump(mode="json"),
                "segment_name": state["segment"]["name"],
            },
            AlertExplanation,
            explanation_fallback,
            prompt_version=f"{PROMPT_VERSION}-alert",
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        final_degraded = bool(synthesis_degraded or explanation.degraded)
        if final_degraded and not synthesis_output.degraded:
            synthesis_output = synthesis_output.model_copy(
                update={"confidence": "low", "degraded": True}
            )
        provenance = list(state.get("provenance", []))
        provenance.extend(
            [
                {
                    "node": "finalize",
                    "output": "synthesis",
                    "prompt_version": f"{PROMPT_VERSION}-final",
                    **synthesis.provenance(),
                },
                {
                    "node": "finalize",
                    "output": "alert",
                    "prompt_version": f"{PROMPT_VERSION}-alert",
                    **explanation.provenance(),
                },
            ]
        )
        return {
            "synthesis": synthesis_output.model_dump(mode="json"),
            "alert_explanation": explanation.output.model_dump(mode="json"),
            "provenance": provenance,
            "degraded": final_degraded,
            "status": "investigation_complete",
        }
