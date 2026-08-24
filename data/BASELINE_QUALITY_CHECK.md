# Dataset Quality Sanity Check

This is **not** the final hackathon result. It is a simple baseline check proving the generated benchmark is learnable but not perfectly trivial.

## Baseline protocol
- Model: XGBoost
- Training: train split only
- Threshold selection: validation split only
- Threshold rule: maximize validation recall subject to **>=98% validation precision**
- Final metrics: untouched chronological test split

## Held-out baseline result
- Selected threshold: **0.951**
- Test precision: **0.856**
- Test recall: **0.890**
- Test F1: **0.873**
- Test PR-AUC: **0.952**
- Test ROC-AUC: **0.997**
- False positives: **41**
- Synthetic false-positive financial cost: **₹8,699.67**
- False negatives: **30**
- Synthetic fraud loss missed: **₹197,081.17**
- Synthetic fraud exposure captured by detected positives: **₹974,477.96**

The held-out test period intentionally contains harder/new fraud regimes and mild concept drift. Final project metrics should be produced independently by the project's actual modeling and threshold-selection pipeline.
