# Dataset Card — Multi-Agentic AI Fraud Spike Detector Synthetic Benchmark

## Purpose
A synthetic benchmark created specifically for a defensive, real-time fraud-spike detector. It supports:
- transaction-level fraud scoring,
- streaming fraud-spike detection,
- segment/root-cause investigation,
- held-out precision and recall,
- false-positive financial-cost evaluation,
- concept-drift testing,
- human-in-the-loop response-policy experiments.

## Size and splits
- **30,000 transactions total**
- **Train:** 18,000 transactions
- **Validation:** 6,000 transactions
- **Held-out test:** 6,000 transactions
- Split is **chronological**, not random.

The test label files are intentionally separated and prefixed `DO_NOT_USE_FOR_TUNING_`.

## Why the split is chronological
Fraud systems are temporal systems. A random split can leak future behavior into training and make evaluation unrealistically easy. The held-out period also contains new fraud patterns and mild concept drift.

## Fraud-spike structure
The stream contains:
- normal background fraud,
- several injected coordinated fraud-spike periods,
- legitimate high-volume sale/salary surges designed to create false-positive pressure,
- harder, partially novel fraud regimes in the test period.

Individual transactions inside a spike are **not all fraudulent**, which prevents the detector from treating an entire time window as automatically malicious.

## Label separation
Feature files contain no target column.

Label sidecars contain:
- `is_fraud`
- `is_within_spike_window`
- `is_spike_injected`
- event IDs
- scenario family (evaluation only)
- false-positive cost if a genuine transaction is blocked
- fraud loss if a fraudulent transaction is missed

## Evaluation protocol
Use train data to fit models.
Use validation data for threshold selection, calibration, model choice, and policy tuning.
Use the held-out test labels **once for final reporting**.

Primary hackathon metrics:
- Precision
- Recall
- F1
- PR-AUC
- False-positive financial cost

Recommended additional metrics:
- event-level spike precision/recall,
- detection delay,
- fraud value captured,
- fraud value missed,
- legitimate value impacted,
- analyst escalation rate.

## Synthetic-data disclosure
This dataset is fully synthetic. It contains no real people, cards, accounts, merchants, or payment credentials. IDs are artificial and non-identifying.

The class prevalence is intentionally enriched relative to many production fraud streams so that a 30k-row hackathon dataset contains enough positive examples for statistically useful held-out precision/recall.

The financial cost fields are synthetic evaluation proxies and must not be represented as Razorpay's real cost model.

## Defense-only scope
The dataset is designed to evaluate detection, verification, monitoring, and defensive response systems. It does not contain instructions or operational details intended to facilitate fraud.
