"""Independently suggest verdicts, then enforce evidence resolution in Python."""

from __future__ import annotations

from pathlib import Path

from backend.app.agents.state import (
    InvestigationState,
    VerificationResult,
    VerificationSuggestion,
    VerificationSuggestions,
    VerificationVerdict,
)
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier

PROMPT_VERSION = "verification-v1"
PROMPT = Path(__file__).parents[1].joinpath("prompts/verification_v1.txt").read_text(
    encoding="utf-8"
)


class VerificationAgent:
    def __init__(self, gateway: StructuredLLMGateway) -> None:
        self.gateway = gateway

    async def __call__(self, state: InvestigationState) -> dict[str, object]:
        hypotheses = state["hypotheses"]

        def fallback() -> VerificationSuggestions:
            return VerificationSuggestions(
                verdicts=[
                    VerificationSuggestion(
                        claim_id=str(item["claim_id"]),
                        verdict="unsupported",
                        rationale="Independent verification is unavailable; fail closed pending analyst review.",
                    )
                    for item in hypotheses
                ]
            )

        result = await self.gateway.generate(
            ModelTier.SECONDARY,
            PROMPT,
            {"hypotheses": hypotheses, "evidence_store": state["evidence"]},
            VerificationSuggestions,
            fallback,
            prompt_version=PROMPT_VERSION,
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        claim_ids = [str(item["claim_id"]) for item in hypotheses]
        duplicate_claim_ids = {
            claim_id for claim_id in claim_ids if claim_ids.count(claim_id) > 1
        }
        suggestion_ids = [item.claim_id for item in result.output.verdicts]
        suggestion_by_claim = {item.claim_id: item for item in result.output.verdicts}
        available = {str(item["evidence_id"]) for item in state["evidence"]}
        verdicts: list[VerificationVerdict] = []
        for hypothesis in hypotheses:
            claim_id = str(hypothesis["claim_id"])
            cited = [str(item) for item in hypothesis["evidence_ids"]]
            resolved = [item for item in cited if item in available]
            suggestion = suggestion_by_claim.get(claim_id)
            if claim_id in duplicate_claim_ids:
                verdict = "unsupported"
                rationale = "Duplicate claim IDs cannot receive shared verification credit."
            elif suggestion_ids.count(claim_id) != 1:
                verdict = "unsupported"
                rationale = "The independent verifier must return exactly one verdict per claim."
            elif not cited or len(resolved) != len(cited):
                verdict = "unsupported"
                rationale = "One or more cited evidence IDs do not resolve."
            elif suggestion is None:
                verdict = "unsupported"
                rationale = "The independent verifier returned no verdict for this claim."
            else:
                verdict = suggestion.verdict
                rationale = suggestion.rationale
            verdicts.append(
                VerificationVerdict(
                    claim_id=claim_id,
                    verdict=verdict,
                    resolved_evidence_ids=resolved,
                    rationale=rationale,
                )
            )
        supported = sum(item.verdict == "supported" for item in verdicts)
        verification = VerificationResult(
            verdicts=verdicts,
            grounding_score=supported / max(len(verdicts), 1),
            rejected_claim_count=len(verdicts) - supported,
        )
        provenance = list(state.get("provenance", []))
        provenance.append(
            {"node": "verify_evidence", "prompt_version": PROMPT_VERSION, **result.provenance()}
        )
        return {
            "verification": verification.model_dump(mode="json"),
            "provenance": provenance,
            "degraded": bool(state.get("degraded", False) or result.degraded),
        }
