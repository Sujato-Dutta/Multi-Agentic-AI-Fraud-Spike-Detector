"""Exact evidence-grounded investigation graph with a durable human-review interrupt."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from prometheus_client import Counter

from backend.app.agents.nodes import (
    EvidenceAgent,
    LeadInvestigator,
    ResponseAgent,
    SegmentAgent,
    VerificationAgent,
)
from backend.app.agents.state import InvestigationState
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.safety.evidence_grounding import (
    build_authorization_context,
    ground_claims,
)
from backend.app.safety.metrics import record_degradation
from backend.app.safety.policy_engine import PolicyEngine

POLICY_DECISIONS = Counter(
    "fraud_policy_decisions_total",
    "Deterministic policy outcomes.",
    ("decision", "rule_id"),
)


def build_investigation_graph(
    gateway: StructuredLLMGateway,
    checkpointer: Any,
    policy_engine: PolicyEngine | None = None,
    response_policy: Any | None = None,
):
    policy = policy_engine or PolicyEngine.default()
    lead = LeadInvestigator(gateway)
    segment = SegmentAgent(gateway)
    evidence = EvidenceAgent(gateway)
    verifier = VerificationAgent(gateway)
    response = ResponseAgent(gateway, response_policy)

    async def observe(state: InvestigationState) -> dict[str, object]:
        return {"status": "observed", "degraded": bool(state.get("degraded", False))}

    async def retrieve_evidence(state: InvestigationState) -> dict[str, object]:
        if not state.get("evidence"):
            raise ValueError("Investigation requires deterministic evidence")
        return {"status": "evidence_retrieved"}

    async def estimate_impact(state: InvestigationState) -> dict[str, object]:
        if not state.get("impact"):
            raise ValueError("Impact must be computed deterministically before graph execution")
        return {"status": "impact_estimated"}

    async def policy_gate(state: InvestigationState) -> dict[str, object]:
        grounded = ground_claims(
            state["hypotheses"], state["evidence"], state["verification"]
        )
        action = str(state["responses"][0]["action"])
        context, basis = build_authorization_context(
            action, state["evidence"], state["impact"]
        )
        decision = policy.evaluate(action, context)
        POLICY_DECISIONS.labels(
            decision=decision.decision, rule_id=decision.rule_id
        ).inc()
        if decision.decision == "deny":
            record_degradation("policy_violation")
        return {
            "grounded_claims": grounded,
            "policy_context": context.model_dump(mode="json"),
            "policy_basis": basis,
            "policy_gate": {
                **decision.model_dump(mode="json"),
                "authorized": False,
                "recommended_action": action,
            },
            "status": "awaiting_human_review",
        }

    def human_review(state: InvestigationState) -> dict[str, object]:
        review = interrupt(
            {
                "incident_id": state["incident_id"],
                "segment": state["segment"],
                "verification": state["verification"],
                "grounded_claims": state["grounded_claims"],
                "impact": state["impact"],
                "responses": state["responses"],
                "policy_context": state["policy_context"],
                "policy_basis": state["policy_basis"],
                "policy_gate": state["policy_gate"],
            }
        )
        return {"human_review": dict(review), "status": "human_review_recorded"}

    async def capture_outcome(_: InvestigationState) -> dict[str, object]:
        return {"status": "awaiting_outcome"}

    workflow = StateGraph(InvestigationState)
    workflow.add_node("observe", observe)
    workflow.add_node("retrieve_evidence", retrieve_evidence)
    workflow.add_node("analyze_spike", lead.analyze)
    workflow.add_node("discover_segment", segment)
    workflow.add_node("investigate_root_cause", evidence)
    workflow.add_node("verify_evidence", verifier)
    workflow.add_node("estimate_impact", estimate_impact)
    workflow.add_node("evaluate_responses", response)
    workflow.add_node("policy_gate", policy_gate)
    workflow.add_node("human_review", human_review)
    workflow.add_node("finalize", lead.finalize)
    workflow.add_node("capture_outcome", capture_outcome)
    ordered = (
        "observe",
        "retrieve_evidence",
        "analyze_spike",
        "discover_segment",
        "investigate_root_cause",
        "verify_evidence",
        "estimate_impact",
        "evaluate_responses",
        "policy_gate",
        "human_review",
        "finalize",
        "capture_outcome",
    )
    workflow.add_edge(START, ordered[0])
    for source, target in pairwise(ordered):
        workflow.add_edge(source, target)
    workflow.add_edge(ordered[-1], END)
    return workflow.compile(checkpointer=checkpointer)
