"""Explain an immutable production-policy ranking; never authorize or reorder it."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from backend.app.agents.state import (
    InvestigationState,
    ResponsePlan,
    ResponseRecommendation,
)
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.llm.routing import ModelTier
from backend.app.ml.policy.shadow_policy import ShadowPolicy, policy_context_from_state

PROMPT_VERSION = "response-v2"
PROMPT = Path(__file__).parents[1].joinpath("prompts/response_v1.txt").read_text(
    encoding="utf-8"
)


class ResponseAgent:
    def __init__(
        self, gateway: StructuredLLMGateway, response_policy: Any | None = None
    ) -> None:
        self.gateway = gateway
        self.response_policy = response_policy or ShadowPolicy(None)

    async def __call__(self, state: InvestigationState) -> dict[str, object]:
        evidence_ids = [
            str(item["evidence_id"])
            for item in state["evidence"]
            if item["evidence_type"] in {"window_statistics", "segment_statistics"}
        ]
        cited = evidence_ids[:2] or [str(state["evidence"][0]["evidence_id"])]
        policy_result = self.response_policy.score(policy_context_from_state(state))
        if inspect.isawaitable(policy_result):
            policy_result = await policy_result
        ranking = [
            str(item["action"]) for item in policy_result["production_ranking"]
        ]

        def fallback() -> ResponsePlan:
            return ResponsePlan(
                responses=[
                    ResponseRecommendation(
                        rank=rank,
                        action=action,
                        rationale=(
                            "Production response policy ranking; deterministic safety and human review remain required."
                        ),
                        evidence_ids=cited,
                    )
                    for rank, action in enumerate(ranking, start=1)
                ]
            )

        result = await self.gateway.generate(
            ModelTier.SECONDARY,
            PROMPT,
            {
                "segment": state["segment"],
                "verified_claims": state["verification"],
                "deterministic_impact": state["impact"],
                "immutable_production_ranking": ranking,
                "instruction": "Explain this ranking without changing its actions or order.",
            },
            ResponsePlan,
            fallback,
            prompt_version=PROMPT_VERSION,
            incident_id=state["incident_id"],
            trace_id=state["trace_id"],
        )
        available = {str(item["evidence_id"]) for item in state["evidence"]}
        explanations = {item.action: item for item in result.output.responses}
        responses = []
        for rank, action in enumerate(ranking, start=1):
            proposed = explanations.get(action)
            responses.append(
                ResponseRecommendation(
                    rank=rank,
                    action=action,
                    rationale=(
                        proposed.rationale
                        if proposed is not None
                        else "Production response policy ranking; explanation unavailable."
                    ),
                    requires_human_review=True,
                    evidence_ids=(
                        list(
                            dict.fromkeys(
                                evidence_id
                                for evidence_id in proposed.evidence_ids
                                if evidence_id in available
                            )
                        )
                        if proposed is not None
                        else cited
                    )
                    or cited,
                )
            )
        provenance = list(state.get("provenance", []))
        provenance.append(
            {"node": "evaluate_responses", "prompt_version": PROMPT_VERSION, **result.provenance()}
        )
        return {
            "responses": [item.model_dump(mode="json") for item in responses],
            "response_policy": policy_result,
            "provenance": provenance,
            "degraded": bool(
                state.get("degraded", False)
                or result.degraded
                or policy_result["degraded"]
            ),
        }
