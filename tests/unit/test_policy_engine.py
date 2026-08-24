from backend.app.safety.evidence_grounding import build_authorization_context
from backend.app.safety.policy_engine import PolicyEngine


def _context(**overrides):
    values = {
        "affected_legitimate_value_inr": 1_000.0,
        "fraud_exposure_inr": 10_000.0,
        "segment_breadth": 0.05,
        "grounding_score": 1.0,
        "confidence_score": 1.0,
        "novelty_score": 0.0,
        "actor_role": "lead_analyst",
    }
    values.update(overrides)
    return values


def test_broad_block_above_legitimate_value_ceiling_is_denied() -> None:
    result = PolicyEngine.default().evaluate(
        "temporary_defensive_rule",
        _context(affected_legitimate_value_inr=50_001.0),
    )
    assert result.decision == "deny"
    assert result.rule_id == "legitimate_value_ceiling"


def test_same_action_under_ceiling_requires_approval() -> None:
    result = PolicyEngine.default().evaluate(
        "temporary_defensive_rule", _context()
    )
    assert result.decision == "require_approval"


def test_low_grounding_forces_escalation() -> None:
    result = PolicyEngine.default().evaluate(
        "temporary_defensive_rule", _context(grounding_score=0.2)
    )
    assert result.decision == "deny"
    assert result.escalation == "escalate"


def test_role_action_mismatch_is_denied() -> None:
    result = PolicyEngine.default().evaluate(
        "temporary_defensive_rule", _context(actor_role="analyst")
    )
    assert result.decision == "deny"
    assert result.rule_id == "role_action_mismatch"


def test_unknown_action_is_denied_by_default() -> None:
    result = PolicyEngine.default().evaluate("llm_invented_action", _context())
    assert result.decision == "deny"
    assert result.rule_id == "unknown_action"


def _deterministic_bundle(*, window_count: int = 100, support: int = 5):
    impact = {
        "segment_name": "is_proxy_ip=true",
        "transaction_count": support,
        "fraud_exposure_inr": 10_000.0,
        "false_positive_exposure_inr": 10.0,
        "affected_legitimate_value_inr": 1_000.0,
        "calculation_method": "deterministic_probability_weighted",
    }
    evidence = [
        {
            "evidence_id": "window",
            "evidence_type": "window_statistics",
            "source": "detector_window",
            "payload": {"transaction_count": window_count},
        },
        {
            "evidence_id": "segment",
            "evidence_type": "segment_statistics",
            "source": "deterministic_segmentation",
            "payload": {"rank": 1, "name": "is_proxy_ip=true", "support": support},
        },
        {
            "evidence_id": "impact",
            "evidence_type": "impact_estimate",
            "source": "deterministic_cost_engine",
            "payload": impact,
        },
        {
            "evidence_id": "similar",
            "evidence_type": "similar_incidents",
            "source": "historical_incident_store",
            "payload": {"count": 2},
        },
    ]
    return evidence, impact


def test_authorization_context_uses_full_window_and_deterministic_records() -> None:
    evidence, impact = _deterministic_bundle()
    context, basis = build_authorization_context(
        "temporary_defensive_rule", evidence, impact, actor_role="lead_analyst"
    )
    assert context.segment_breadth == 0.05
    assert context.grounding_score == context.confidence_score == 1
    assert basis["valid"] is True
    assert PolicyEngine.default().evaluate(
        "temporary_defensive_rule", context
    ).decision == "require_approval"


def test_missing_deterministic_record_fails_closed_regardless_of_model_claims() -> None:
    evidence, impact = _deterministic_bundle()
    context, basis = build_authorization_context(
        "temporary_defensive_rule", evidence[:-2], impact, actor_role="lead_analyst"
    )
    decision = PolicyEngine.default().evaluate("temporary_defensive_rule", context)
    assert basis["valid"] is False
    assert context.grounding_score == context.confidence_score == 0
    assert decision.decision == "deny"
    assert decision.rule_id == "grounding_score_floor"


def test_malformed_deterministic_numbers_cannot_normalize_into_authorization() -> None:
    evidence, impact = _deterministic_bundle()
    evidence[0]["payload"]["transaction_count"] = 100.5
    evidence[1]["payload"]["support"] = False
    evidence[3]["payload"]["count"] = -0.5
    impact["transaction_count"] = False
    context, basis = build_authorization_context(
        "temporary_defensive_rule", evidence, impact, actor_role="lead_analyst"
    )
    assert basis["valid"] is False
    assert context.grounding_score == context.confidence_score == 0
