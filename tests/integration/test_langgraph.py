from __future__ import annotations

import time

import numpy as np
import orjson
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.agents.graph import build_investigation_graph
from backend.app.agents.state import (
    AlertExplanation,
    HypothesisSet,
    LeadSynthesis,
    ResponsePlan,
    SegmentInterpretation,
    SpikeAnalysis,
    VerificationSuggestions,
)
from backend.app.db.repositories import IncidentRepository, TransactionRepository
from backend.app.llm.gateway import StructuredLLMGateway, TokenUsage
from backend.app.ml.spike_detection.detector import SpikeAlert
from backend.app.services.investigation_service import InvestigationService
from evaluation.dataio import load_features

pytestmark = pytest.mark.asyncio


async def _stubbed_model(tier, system_prompt, data_block, schema):
    payload = orjson.loads(data_block.splitlines()[1])
    if schema is SpikeAnalysis:
        ids = [item["evidence_id"] for item in payload["evidence"]]
        output = {
            "summary": "Risk density rose above its historical baseline.",
            "anomalies": ["Elevated calibrated density", "Elevated high-risk count"],
            "evidence_ids": ids,
        }
    elif schema is SegmentInterpretation:
        item = payload["segment_evidence"][0]
        output = {
            "name": item["payload"]["name"],
            "description": "The deterministic top-ranked cohort concentrates elevated risk.",
            "conditions": item["payload"].get("conditions", []),
            "evidence_ids": [item["evidence_id"]],
        }
    elif schema is HypothesisSet:
        evidence = payload["evidence_store"]
        output = {
            "hypotheses": [
                {
                    "claim_id": "CLM-INTEGRATION-1",
                    "statement": "The calibrated risk-density increase is statistically material.",
                    "evidence_ids": [evidence[0]["evidence_id"]],
                    "strength": "strong",
                },
                {
                    "claim_id": "CLM-INTEGRATION-2",
                    "statement": "The named deterministic segment is a major contributor.",
                    "evidence_ids": [
                        next(
                            item["evidence_id"]
                            for item in evidence
                            if item["evidence_type"] == "segment_statistics"
                        )
                    ],
                    "strength": "moderate",
                },
            ]
        }
    elif schema is VerificationSuggestions:
        output = {
            "verdicts": [
                {
                    "claim_id": item["claim_id"],
                    "verdict": "supported",
                    "rationale": "The cited deterministic evidence resolves.",
                }
                for item in payload["hypotheses"]
            ]
        }
    elif schema is ResponsePlan:
        ids = payload["segment"]["evidence_ids"]
        output = {
            "responses": [
                {
                    "rank": 1,
                    "action": "human_escalation",
                    "rationale": "Escalate before any defensive action.",
                    "requires_human_review": True,
                    "evidence_ids": ids,
                },
                {
                    "rank": 2,
                    "action": "enhanced_monitoring",
                    "rationale": "Monitor the affected cohort.",
                    "requires_human_review": True,
                    "evidence_ids": ids,
                },
            ]
        }
    elif schema is LeadSynthesis:
        ids = [item for hypothesis in payload["hypotheses"] for item in hypothesis["evidence_ids"]]
        output = {
            "summary": "Two evidence-backed hypotheses support analyst escalation.",
            "confidence": "high",
            "escalation_posture": "escalate",
            "evidence_ids": sorted(set(ids)),
            "degraded": False,
        }
    elif schema is AlertExplanation:
        output = {
            "title": "Fraud-risk spike investigated",
            "analyst_summary": payload["synthesis"]["summary"],
            "next_step": "Review ranked responses.",
        }
    else:
        raise AssertionError(schema)
    return schema.model_validate(output), TokenUsage(20, 10)


async def _create_incident(stack) -> str:
    frame = load_features("validation").iloc[:12]
    async with stack.session_factory() as session:
        transactions = TransactionRepository(session)
        for _, row in frame.iterrows():
            await transactions.insert_with_score(
                row.to_dict(),
                {
                    "risk_probability": 0.8,
                    "decision_score": 0.9,
                    "decision_threshold": 0.4,
                    "score_space": "rank_preserving_isotonic_probability",
                    "degraded": False,
                },
            )
        start = frame.iloc[0]["timestamp"] - np.timedelta64(1, "s")
        end = frame.iloc[-1]["timestamp"]
        alert = SpikeAlert(
            alert_id="SPIKE-20260525-9001",
            fire_timestamp=end,
            window_start=start,
            transaction_count=len(frame),
            risk_density=0.8,
            baseline_density=0.02,
            density_lift=40.0,
            high_risk_count=len(frame),
            expected_high_risk_rate=0.02,
            p_value=1e-9,
            volume_lift=1.0,
            promo_share=0.0,
            required_lift=2.5,
            reason="integration spike",
            drift_psi=0.1,
        )
        await IncidentRepository(session).create(
            incident_id="INC-20260525-9001",
            alert=alert,
            segments=[
                {
                    "conditions": ["payment_method=card"],
                    "support": 6,
                    "baseline_support": 40,
                    "risk_density": 0.8,
                    "baseline_risk_density": 0.02,
                    "density_lift": 40.0,
                    "prevalence_lift": 2.0,
                    "excess_risk_contribution": 0.7,
                    "p_value": 1e-6,
                    "score": 12.0,
                    "condition_contributions": [],
                }
            ],
            trace_id="trace-integration",
        )
        await session.commit()
    return "INC-20260525-9001"


async def test_full_graph_persists_grounded_investigation_and_warm_cache_p95(app_stack) -> None:
    incident_id = await _create_incident(app_stack)
    gateway = StructuredLLMGateway(
        app_stack.cache,
        app_stack.settings.model_copy(update={"gemini_api_key": "stub-key"}),
        invoker=_stubbed_model,
        state=app_stack.state,
    )
    service = InvestigationService(
        build_investigation_graph(gateway, InMemorySaver()),
        app_stack.settings,
        session_factory=app_stack.session_factory,
    )
    paused = await service.investigate(incident_id)
    assert paused["status"] == "awaiting_human_review"
    assert paused["segment"]["name"] == "payment_method=card"
    assert len(paused["hypotheses"]) == 2
    assert all(item["evidence_ids"] for item in paused["hypotheses"])
    assert all(
        item["verdict"] == "supported" for item in paused["verification"]["verdicts"]
    )
    assert paused["verification"]["grounding_score"] == 1.0
    assert paused["impact"]["fraud_exposure_inr"] > 0
    assert paused["impact"]["false_positive_exposure_inr"] > 0
    assert [
        (item["rank"], item["action"]) for item in paused["responses"]
    ] == [
        (rank, item["action"])
        for rank, item in enumerate(
            paused["response_policy"]["production_ranking"], start=1
        )
    ]
    final = await service.resume(
        incident_id, {"decision": "defer_to_phase5", "actor": "integration-test"}
    )
    assert final["synthesis"]["confidence"] == "low"
    assert paused["response_policy"]["degraded"] is True
    assert final["policy_gate"]["authorized"] is False

    async with app_stack.session_factory() as session:
        repository = IncidentRepository(session)
        evidence = await repository.list_evidence(incident_id)
        outputs = await repository.list_agent_outputs(incident_id)
    assert len(evidence) >= 5
    names = {row.agent_name for row in outputs}
    assert {
        "lead_spike_analysis",
        "segment_interpretation",
        "root_cause_hypotheses",
        "evidence_verification",
        "deterministic_impact",
        "response_recommendations",
        "lead_synthesis",
        "alert_explanation",
    }.issubset(names)

    latencies = []
    for _ in range(3):
        warm = InvestigationService(
            build_investigation_graph(gateway, InMemorySaver()),
            app_stack.settings,
            session_factory=app_stack.session_factory,
        )
        started = time.perf_counter()
        await warm.investigate(incident_id)
        await warm.resume(incident_id, {"decision": "defer_to_phase5", "actor": "warm-cache"})
        latencies.append(time.perf_counter() - started)
    assert float(np.percentile(latencies, 95)) <= 20.0
