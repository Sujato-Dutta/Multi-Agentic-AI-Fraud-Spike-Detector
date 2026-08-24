# Phase 6 Shadow Policy Validation

Development validation holdback only; held-out test labels were not accessed.
Action effects are explicit assumptions, not observed treatment effects.

| Policy | Expected reward | Recall | FP cost | Safety violations |
|---|---:|---:|---:|---:|
| production | ₹94,622.18 | 0.450 | ₹0.00 | 0 |
| candidate | ₹170,890.34 | 0.920 | ₹18,047.92 | 1 |
| always_escalate | ₹94,622.18 | 0.450 | ₹0.00 | 0 |
| always_step_up | ₹138,176.71 | 0.700 | ₹2,707.19 | 1 |

Promotion gate passed: **False**. Promotion remains an explicit admin action; no automatic path exists.