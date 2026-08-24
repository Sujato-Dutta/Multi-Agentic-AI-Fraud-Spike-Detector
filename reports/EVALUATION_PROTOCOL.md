# Evaluation Protocol

## Split discipline

- Fit model parameters and category handling on the chronological training split only.
- Fit isotonic calibration on the final 10% of training time, while fitting the base model on the
  first 90%.
- Select the operating threshold and detector parameters on validation only.
- Use the chronological train-tail dev-test (`2026-05-11` onward) for pipeline dry-runs; when used,
  fit its temporary model only on rows before that date.
- Do not open raw held-out test labels during Phases 1–7. Phase 8 uses the guarded loader once.
- Never random-shuffle across time or use scenario-family labels for tuning.

The dataset already includes historical velocities, customer aggregates, z-scores, and merchant
risk rates. This project assumes those supplied features were calculated using only information
available at transaction time; their generator provenance cannot be re-derived from these files.

## Leakage controls

The nine evaluation-only columns and raw IDs are forbidden model inputs. `transaction_id` is only a
join key. `timestamp` is retained for chronological replay but is not a raw model feature.
`ip_cluster_id` contributes only its low-cardinality prefix as `ip_cluster_group`.

## Transaction metrics

- Precision = TP / (TP + FP)
- Recall = TP / (TP + FN)
- F1 = harmonic mean of precision and recall
- False-positive rate = FP / (FP + TN)
- False-negative rate = FN / (FN + TP)
- PR-AUC and ROC-AUC use continuous rank-preserving isotonic probabilities. Raw-score PR-AUC and
  ROC-AUC are also reported explicitly as model-discrimination diagnostics.
- FP cost, fraud loss missed, and exposure captured are direct sums of dataset cost columns.

## Spike-event metrics

An alert matches an event when its first fire time lies inclusively in
`[event.start, event.end + 30 minutes]`. First match wins. Further alerts overlapping the same event
are continuations and excluded from event precision. Unmatched alerts are false alerts. Benign-surge
false alerts are reported separately.

- Event precision = matched alerts / (matched alerts + false alerts). Defined as 1.0 when both are 0.
- Event recall = matched events / total events. Defined as 1.0 when both are 0.
- Detection delay = alert fire time − event start; report mean, median, and P90 minutes.
- False alerts per day = unmatched alerts / evaluated stream duration in days.

## Operating points

The primary operating point maximizes validation recall subject to precision ≥ 0.90 in raw XGBoost
probability score space; this keeps the operating threshold independent of calibration shape.
Rank-preserving isotonic probabilities are used for aggregate risk density and protocol ranking
metrics. A second raw-score point minimizes synthetic FP cost plus missed-fraud loss. The full
threshold curve and validated score-space metadata are retained. A failed precision constraint is
reported as such rather than silently relaxing the floor.

## Holdout freeze

`evaluation.dataio.load_test_holdout` requires an explicit acknowledgement token and is only called
by the future Phase 8 held-out report. That run records its commit and timestamp. Existing benchmark
numbers in `data/BASELINE_QUALITY_CHECK.md` are dataset sanity checks and are never represented as
this project's results.
