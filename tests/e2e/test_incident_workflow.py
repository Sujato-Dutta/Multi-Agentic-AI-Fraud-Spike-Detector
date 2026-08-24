from __future__ import annotations

import pytest

from backend.app.db.repositories import (
    AuditRepository,
    FeedbackRepository,
    IncidentRepository,
)
from backend.app.safety.policy_engine import PolicyEngine
from backend.app.schemas import ReviewDecisionRequest, UserIdentity
from evaluation.dataio import load_features
from tests.conftest import analyst_headers

pytestmark = pytest.mark.asyncio


async def test_incident_rejection_audit_and_feedback_lifecycle(
    app_stack, monkeypatch
) -> None:
    validation = load_features("validation")
    for start in range(0, len(validation), app_stack.settings.max_ingest_batch_size):
        rows = validation.iloc[start : start + app_stack.settings.max_ingest_batch_size]
        await app_stack.service.ingest_batch(
            [row.to_dict() for _, row in rows.iterrows()], "trace-e2e-phase5"
        )

    async with app_stack.session_factory() as session:
        incidents = await IncidentRepository(session).list(limit=10)
    assert incidents
    incident_id = incidents[0].incident_id
    paused = await app_stack.app.state.investigation_service.investigate(incident_id)
    assert paused["status"] == "awaiting_human_review"
    assert paused["policy_gate"]["authorized"] is False

    async with app_stack.session_factory() as session:
        repository = IncidentRepository(session)
        outputs = await repository.list_agent_outputs(incident_id)
        hypotheses = next(
            row for row in outputs if row.agent_name == "root_cause_hypotheses"
        )
        verification = next(
            row for row in outputs if row.agent_name == "evidence_verification"
        )
        rejected_claim_id = hypotheses.payload["result"][0]["claim_id"]
        verification_result = dict(verification.payload["result"])
        verification_result["verdicts"] = [
            {**item, "verdict": "unsupported"}
            if item["claim_id"] == rejected_claim_id
            else item
            for item in verification_result["verdicts"]
        ]
        verification.payload = {
            **verification.payload,
            "result": verification_result,
        }
        await session.commit()

    headers = await analyst_headers(app_stack)
    for path in (
        f"/api/incidents/{incident_id}",
        f"/api/incidents/{incident_id}/investigation",
    ):
        response = await app_stack.client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        root_outputs = [
            item
            for item in (
                response.json()["timeline"]
                if path.endswith(incident_id)
                else response.json()["outputs"]
            )
            if (
                item.get("kind") == "agent_output"
                and item["payload"].get("agent_name") == "root_cause_hypotheses"
            )
            or item.get("agent_name") == "root_cause_hypotheses"
        ]
        visible = root_outputs[0]["payload"]
        if "payload" in visible:
            visible = visible["payload"]
        assert rejected_claim_id not in {
            item["claim_id"] for item in visible["result"]
        }

    async with app_stack.session_factory() as session:
        raw_outputs = await IncidentRepository(session).list_agent_outputs(incident_id)
    raw_hypotheses = next(
        row for row in raw_outputs if row.agent_name == "root_cause_hypotheses"
    )
    assert rejected_claim_id in {
        item["claim_id"] for item in raw_hypotheses.payload["result"]
    }

    review = await app_stack.client.get(
        f"/api/decisions/{incident_id}/review", headers=headers
    )
    assert review.status_code == 200, review.text
    panel = review.json()
    assert panel["choices"] == ["approve", "modify", "reject", "escalate"]
    assert panel["recommendation"]
    assert panel["grounded_claims"] is not None
    assert panel["impact"]["fraud_exposure_inr"] >= 0

    original_append = AuditRepository.append_once
    crash_once = True

    async def fail_after_checkpoint(self, **kwargs):
        nonlocal crash_once
        if kwargs["event_type"] == "investigation_resumed" and crash_once:
            crash_once = False
            raise RuntimeError("simulated post-resume crash")
        return await original_append(self, **kwargs)

    monkeypatch.setattr(AuditRepository, "append_once", fail_after_checkpoint)
    with pytest.raises(RuntimeError, match="simulated post-resume crash"):
        await app_stack.app.state.review_service.decide(
            incident_id,
            ReviewDecisionRequest(
                decision="reject", reason_code="false_positive"
            ),
            UserIdentity(
                username=app_stack.settings.demo_analyst_username,
                role="analyst",
            ),
        )
    monkeypatch.setattr(AuditRepository, "append_once", original_append)
    current_policy = app_stack.app.state.review_service.policy
    monkeypatch.setattr(
        app_stack.app.state.review_service,
        "policy",
        PolicyEngine(
            current_policy.rules.model_copy(update={"version": "safety-v-next"})
        ),
    )

    rejected = await app_stack.client.post(
        f"/api/decisions/{incident_id}",
        headers=headers,
        json={"decision": "reject", "reason_code": "false_positive"},
    )
    assert rejected.status_code == 200, rejected.text
    decision = rejected.json()
    assert decision["decision"] == "reject"
    assert decision["final_action"]["action"] == "no_action"
    assert decision["final_action"]["authorized"] is False
    assert decision["final_action"]["authorization_basis"]["valid"] is True
    assert decision["investigation_status"] == "awaiting_outcome"

    duplicate = await app_stack.client.post(
        f"/api/decisions/{incident_id}",
        headers=headers,
        json={"decision": "reject", "reason_code": "false_positive"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["decision_id"] == decision["decision_id"]

    audit = await app_stack.client.get(
        f"/api/decisions/{incident_id}/audit", headers=headers
    )
    assert audit.status_code == 200, audit.text
    assert {
        "policy_evaluated",
        "analyst_decision_recorded",
        "investigation_resumed",
    }.issubset({item["event_type"] for item in audit.json()["events"]})
    assert sum(
        item["event_type"] == "investigation_resumed"
        for item in audit.json()["events"]
    ) == 1

    outcome = await app_stack.client.post(
        f"/api/feedback/{decision['decision_id']}/outcome",
        headers=headers,
        json={"outcome_code": "legitimate", "false_positive_cost_inr": 40.0},
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["status"] == "outcome_recorded"

    async with app_stack.session_factory() as session:
        rows = await FeedbackRepository(session).list_for_incident(incident_id)
        incident = await IncidentRepository(session).get(incident_id)
    assert len(rows) == 1
    assert rows[0].outcome["outcome_code"] == "legitimate"
    assert incident is not None and incident.status == "completed"

    final_audit = await app_stack.client.get(
        f"/api/decisions/{incident_id}/audit", headers=headers
    )
    assert "outcome_recorded" in {
        item["event_type"] for item in final_audit.json()["events"]
    }
