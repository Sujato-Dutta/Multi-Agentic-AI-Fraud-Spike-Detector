# Held-out Evaluation Caveats

Companion to `results.json` and `report.md`. These caveats were identified after the sealed run
finished. They are recorded here rather than by re-running the evaluation, because another run would
mean another read of the sealed labels for no measurement benefit. `results.json` is left exactly as
it was generated.

## 1. Transaction precision fell below the validation floor

Held-out precision was **0.6209** against a validation-selected floor of **0.90**; recall rose to
**0.9780**. The threshold (0.3715, raw XGBoost probability) was frozen into the model artifact before
this run and was not re-tuned afterwards.

Ranking quality held: PR-AUC **0.9485** and ROC-AUC **0.9969**. The loss is therefore in threshold
placement, not in the model's ordering. The project risk register predicted this: the held-out spike
families (`low_amount_microburst`, `trusted_device_takeover_like`, `cross_channel_burst`) are labelled
`difficulty=hard` and are built specifically to defeat the signatures that dominate train and
validation.

## 2. The policy comparison is not a serving-time forecast

The production-versus-candidate numbers (production ₹-916.75, candidate ₹-1,808.80) score both
rankings using the **offline-training** context semantics, which differ from serving in three ways:

| Feature | Held-out report | Serving (`shadow_policy.policy_context_from_state`) |
|---|---|---|
| `historical_segment_fraud_rate` | realized `is_fraud` mean of the alert window (sealed label) | `historical_baseline.expected_high_risk_rate` (a predicted rate) |
| `agent_confidence`, `grounding_score` | pinned to 0.0 | the real verification grounding score |
| `segment_breadth` | window rows ÷ whole-holdout rows | segment support ÷ window transaction count |

Lift features also use whole-holdout means, so they look ahead within the test split. The detector
itself does **not**: it primes a causal EWMA baseline on train and validation only.

Read the comparison as a like-for-like ranking comparison under one fixed feature convention, not as
what either policy would produce in production. Nothing is promoted from it: `automatic_promotion` is
`false`, activation is admin-gated, and the candidate was fit on development data only. The headline
transaction, event, and business metrics do not use these features at all.

## 3. Live agent-narrative metrics were not measured

Grounding accuracy, verification rejection rate, recommendation acceptance rate, and override rate
require the running stack with an LLM credential, which was unavailable at freeze time. The report
records this explicitly instead of estimating. What *is* measured and reported: deterministic policy
violations (0), actions authorized by a model (0), and financial values produced by a model (0).

## 4. Three sealed reads, all disclosed

`ACCESS_LOG.md` lists every read with its reason. Reads 2 and 3 fixed report-generator defects; the
fraud model checksum and every transaction, event, and business metric were identical across all
three. The read counter now parses the log's per-read headings and refuses to run if it cannot parse
the history, so it can no longer understate the count.
