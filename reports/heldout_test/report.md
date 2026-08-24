# Held-out Test Evaluation

Generated `2026-08-23T14:19:48.496282+00:00` · commit `unavailable` · working tree `unavailable`.

Sealed evaluation of the test holdout. Model, thresholds, detector parameters, and policies were frozen before it ran.

Reads of the sealed labels so far: **3** (this read: integrity wording correction in the generator: replaced an inaccurate single-read note with an explicit per-read disclosure. No model, threshold, detector parameter, or policy changed.). Every read and its reason is listed in `ACCESS_LOG.md`.

## Transaction level

| Metric | Value |
|---|---:|
| Operating point | precision_floor |
| Threshold (raw_xgboost_probability) | 0.3715 |
| Precision | 0.6209 |
| Recall | 0.9780 |
| F1 | 0.7596 |
| PR-AUC (calibrated) | 0.9485 |
| ROC-AUC (calibrated) | 0.9969 |
| False positives | 163 |
| False negatives | 6 |
| FP rate | 0.0285 |
| FN rate | 0.0220 |

## Spike / event level

| Metric | Value |
|---|---:|
| Event precision | 1.0000 |
| Event recall | 1.0000 |
| Matched events | 3 / 3 |
| False alerts | 0 |
| **False alerts inside benign surges** | **0** |
| Continuation alerts (excluded) | 0 |
| Median detection delay | 30.0 min |
| P90 detection delay | 30.0 min |

## Business outcome

| Metric | INR |
|---|---:|
| Fraud value captured | 1,153,387.28 |
| Fraud value missed | 18,171.85 |
| False-positive cost | 50,224.68 |
| Legitimate value disrupted | 545,282.60 |
| Analyst review cost (assumption) | 750.00 |
| Customer friction cost (assumption) | 6,520.00 |
| **Net risk benefit** | **1,095,892.60** |

Operational costs are the documented assumptions in `reports/COST_ASSUMPTIONS.md`, not measured values. Sensitivity is reported below.

## Policy and safety

- Evaluated held-out incidents: 3
- Production action: `human_escalation` (learned artifact: False)
- Candidate action (shadow only): `enhanced_monitoring`
- Production expected reward: ₹-916.75
- Candidate expected reward: ₹-1,808.80
- Deterministic safety-policy violations: **0**
- Automatic promotion path: False

- The operative action is the production ranking; the candidate is scored for comparison only and never executes.
- No automatic promotion path exists in code; promotion and rollback require an authenticated admin.
- No production policy artifact is registered, so production is the conservative fixed ranking headed by human_escalation.

## Assumption sensitivity

| Assumption | Value | Net risk benefit (INR) |
|---|---:|---:|
| analyst_review_cost_inr | 0.0 | 1,096,642.60 |
| analyst_review_cost_inr | 125.0 | 1,096,267.60 |
| analyst_review_cost_inr | 250.0 | 1,095,892.60 |
| analyst_review_cost_inr | 500.0 | 1,095,142.60 |
| analyst_review_cost_inr | 1000.0 | 1,093,642.60 |
| customer_friction_cost_inr | 0.0 | 1,102,412.60 |
| customer_friction_cost_inr | 20.0 | 1,099,152.60 |
| customer_friction_cost_inr | 40.0 | 1,095,892.60 |
| customer_friction_cost_inr | 80.0 | 1,089,372.60 |
| customer_friction_cost_inr | 160.0 | 1,076,332.60 |

## Agent metrics

- deterministic_policy_violations: 0
- llm_authorized_actions: 0
- financial_values_produced_by_llm: 0
- live_agent_narrative_metrics: not measured in this run: grounding accuracy, verification rejection rate, recommendation acceptance and override rates require the running stack with an LLM credential, which was unavailable at freeze time

## Integrity

- Held-out labels and benign-window annotations are reachable only through the guarded loader, and this script is their only caller.
- Volume lift is emitted as context only and cannot trigger an alert.
- Promotion context raises the required density lift and never suppresses a qualifying spike.
- Chronology verified: train ends 2026-05-24T23:53:28, validation 2026-05-25T00:00:45 to 2026-06-11T23:54:21, test starts 2026-06-12T00:01:46.
- Git metadata was unavailable in this workspace, so the commit hash and working-tree state could not be recorded. This is disclosed rather than substituted.
- DISCLOSURE: this is read 3 of the held-out labels. Reason for this read: integrity wording correction in the generator: replaced an inaccurate single-read note with an explicit per-read disclosure. No model, threshold, detector parameter, or policy changed.. Every read is listed in ACCESS_LOG.md.

Figures: precision_recall.png, risk_density_timeline.png, sensitivity_review_cost.png

Caveats identified after this run are recorded in `CAVEATS.md` beside this file, including why the policy comparison is not a serving-time forecast.
