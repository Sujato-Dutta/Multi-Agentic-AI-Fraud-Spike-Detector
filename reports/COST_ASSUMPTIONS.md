# Cost Assumptions

All financial values in this project are **synthetic evaluation proxies**. They are not Razorpay,
merchant, or industry-standard cost models.

| Quantity | Source/default | Use |
|---|---:|---|
| False-positive cost | Per-row `false_positive_cost_if_blocked_inr` | Sum over genuine transactions incorrectly blocked |
| Fraud loss | Per-row `fraud_loss_if_missed_inr` | Sum over fraudulent transactions missed or captured |
| Analyst review | ₹250 per reviewed incident | Operational workload proxy |
| Customer friction | ₹40 per stepped-up legitimate customer | User-friction proxy |
| Detection delay | ₹500 per hour | Response-delay sensitivity proxy |

The three operational defaults are configured in `backend/app/config.py`. Future counterfactual
response reports must show these assumptions beside their results and include sensitivity analysis.

`Net Risk Benefit = fraud loss prevented − false-positive cost − review cost − friction cost`.
Detection-delay cost is reported separately so the project-level metric remains identical to the
formula in `.agent/agent.md`; Phase 6 reward learning may include it explicitly.
