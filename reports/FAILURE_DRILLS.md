# Failure Drill Checklist

9/9 drills produced their expected visible degraded state.

Every drill runs in-process, so this checklist is reproducible without Docker, a broker,
or model credentials. Nothing here fails silently: each case sets a dependency state, a
Prometheus counter, or an explicit error.

| Drill | Expected | Observed | Result |
|---|---|---|:--:|
| `fraud_model_missing` | Conservative deterministic rule scoring continues; degraded flag and reason set | degraded=True, score_space=deterministic_conservative_rule_score, risk=0.75 | pass |
| `llm_unavailable` | Deterministic template returns usable output; llm marked degraded | degraded=True, llm=degraded, title='Deterministic incident summary' | pass |
| `policy_artifact_corrupt` | Conservative ranking headed by human_escalation; degradation visible | operative=human_escalation, degraded=True | pass |
| `policy_violation` | Deterministic policy denies the action before execution | decision=deny, rule=legitimate_value_ceiling | pass |
| `postgres_down` | Explicit 503 database_unavailable, postgres marked down, transaction buffered to the stream | error=database_unavailable/503, postgres=down, buffered=['transaction.buffered'] | pass |
| `redis_down` | Requests still succeed from the process-local fallback; redis marked degraded | redis=degraded, cached_read=hit | pass |
| `stream_down` | Explicit stream_unavailable error and stream marked down; API keeps serving | error=stream_unavailable, stream=down | pass |
| `underperforming_candidate_policy` | Promotion gate blocks it; promotion stays an explicit admin action | passed=False, reasons=['recall_tolerance', 'fp_cost_tolerance', 'zero_safety_violations'] | pass |
| `verification_rejection` | Claim stripped, counted, and grounding score reduced | supported=1, rejected=1, grounding=0.50 | pass |

## Scenarios

### fraud_model_missing

- Scenario: Primary fraud model artifact and anomaly fallback both unavailable
- Expected: Conservative deterministic rule scoring continues; degraded flag and reason set
- Observed: degraded=True, score_space=deterministic_conservative_rule_score, risk=0.75
- Evidence: `{"reason": "primary_fraud_model_unavailable:FileNotFoundError:anomaly_fallback_unavailable:FileNotFoundError:conservative_rules_active", "score_space": "deterministic_conservative_rule_score"}`

### llm_unavailable

- Scenario: Every model tier times out (equivalent to a revoked API key)
- Expected: Deterministic template returns usable output; llm marked degraded
- Observed: degraded=True, llm=degraded, title='Deterministic incident summary'
- Evidence: `{"failure_reasons": ["primary:TimeoutError", "secondary:TimeoutError", "economy:TimeoutError"], "model": "deterministic-template"}`

### policy_artifact_corrupt

- Scenario: Production response-policy artifact fails to score
- Expected: Conservative ranking headed by human_escalation; degradation visible
- Observed: operative=human_escalation, degraded=True
- Evidence: `{"production_error": "ValueError: corrupt production artifact"}`

### policy_violation

- Scenario: AI-recommended broad defensive rule exceeds the legitimate-value ceiling
- Expected: Deterministic policy denies the action before execution
- Observed: decision=deny, rule=legitimate_value_ceiling
- Evidence: `{"reason": "Affected legitimate value exceeds the action ceiling.", "policy_version": "safety-v2"}`

### postgres_down

- Scenario: Postgres refuses the transaction write during ingestion
- Expected: Explicit 503 database_unavailable, postgres marked down, transaction buffered to the stream
- Observed: error=database_unavailable/503, postgres=down, buffered=['transaction.buffered']
- Evidence: `{"reason": "OperationalError: (builtins.Exception) postgres unavailable\n[SQL: SELECT 1]\n(Background on this error at: https://sqlalche.me/e/20/e3q8)", "buffered_events": ["transaction.buffered"]}`

### redis_down

- Scenario: Redis refuses every operation mid-run
- Expected: Requests still succeed from the process-local fallback; redis marked degraded
- Observed: redis=degraded, cached_read=hit
- Evidence: `{"reason": "OSError: redis unreachable", "stats": {"hits": 1, "misses": 0, "failures": 3, "fallbacks": 2}}`

### stream_down

- Scenario: Redpanda refuses connections when the producer starts
- Expected: Explicit stream_unavailable error and stream marked down; API keeps serving
- Observed: error=stream_unavailable, stream=down
- Evidence: `{"reason": "OSError: connection refused"}`

### underperforming_candidate_policy

- Scenario: Candidate policy has higher reward but safety violations and worse recall
- Expected: Promotion gate blocks it; promotion stays an explicit admin action
- Observed: passed=False, reasons=['recall_tolerance', 'fp_cost_tolerance', 'zero_safety_violations']
- Evidence: `{"checks": {"reward_margin": true, "recall_tolerance": false, "fp_cost_tolerance": false, "zero_safety_violations": false, "measured_holdback": true}}`

### verification_rejection

- Scenario: Model cites an evidence ID that does not exist in the evidence store
- Expected: Claim stripped, counted, and grounding score reduced
- Observed: supported=1, rejected=1, grounding=0.50
- Evidence: `{"rejected_claims": [{"claim_id": "C2", "reason": "supported"}]}`
