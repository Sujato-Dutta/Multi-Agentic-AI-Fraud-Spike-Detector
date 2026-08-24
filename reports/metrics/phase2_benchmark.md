# Phase 2 Benchmark

This is a development benchmark, **not the held-out test result**. Validation is used for model and
detector selection. The train-tail dev-test model is fit/tuned only on transactions before
`2026-05-11T00:00:00`.

## Validation transaction model

| Metric | Value |
|---|---:|
| Calibrated PR-AUC | 0.9853 |
| Raw-score PR-AUC | 0.9853 |
| Calibrated ROC-AUC | 0.9994 |
| Precision | 0.9026 |
| Recall | 0.9797 |
| F1 | 0.9396 |
| False-positive cost | ₹7,980.69 |
| Fraud loss missed | ₹5,274.46 |
| Fraud exposure captured | ₹607,481.66 |

Raw XGBoost probabilities supply the transaction operating point. Rank-preserving isotonic
probabilities supply the aggregate risk-density signal and the protocol PR-AUC/ROC-AUC; raw ranking
metrics are also reported explicitly. The score spaces are validated by the model artifact.

## Validation spike detector

| Metric | Value |
|---|---:|
| Event precision | 1.0000 |
| Event recall | 1.0000 |
| Matched events | 2 / 2 |
| False alerts | 0 |
| False alerts in benign surge | 0 |
| Median detection delay | 22.5 min |
| P90 detection delay | 28.5 min |

## Chronological train-tail dev-test

| Metric | Value |
|---|---:|
| Event precision | 1.0000 |
| Event recall | 1.0000 |
| Matched events | 1 / 1 |
| False alerts | 0 |
| False alerts in `TRN_B2` | 0 |
| Median detection delay | 15.0 min |

## Integrity

- No raw held-out test labels or test spike events were loaded.
- Volume lift is emitted only as context and does not participate in the trigger predicate.
- Promotion context raises the required density lift and never suppresses a qualifying spike.
- Financial values are synthetic proxies; see `reports/COST_ASSUMPTIONS.md`.
