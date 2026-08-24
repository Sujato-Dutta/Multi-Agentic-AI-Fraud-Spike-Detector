# Multi-Agentic AI Fraud Spike Detector

A fraud-risk operations command center that detects **emerging fraud spikes on calibrated risk
density rather than transaction volume**, investigates them with evidence-verified agents, and lets a
deterministic policy engine plus a human analyst authorize every action.

Every metric in this README is copied from `reports/heldout_test/results.json`, produced by the
guarded held-out evaluation. Nothing is estimated and nothing in the interface is hardcoded.

The sealed labels were read **three times**, each read disclosed with its reason in
`reports/heldout_test/ACCESS_LOG.md`. Reads 2 and 3 fixed defects in the *report generator*; no
model, threshold, detector parameter, or policy changed, and every transaction, event, and business
metric was identical across all three reads. Details in
[Documented deviations](#documented-deviations-from-the-plan).

---

## The problem this solves

Measured directly from the dataset, across all splits:

| Zone | Volume vs mean | Fraud rate |
|---|---:|---:|
| Normal traffic | ×1.0 (≈13.9 txn/h) | 1.69% – 1.83% |
| Fraud spike windows | ×2.3 – ×2.9 | 13.7% – 22.3% |
| Benign surges (festival / flash sale / salary day) | ×2.2 – ×2.6 | 0.8% – 2.3% |

Fraud spikes and benign surges are **volume-indistinguishable**. They differ by roughly 10× in fraud
rate. A volume-triggered detector therefore fires on festival traffic and blocks paying customers.

**The design consequence, enforced in code:** the detector triggers on calibrated fraud probability
density and is structurally incapable of firing on volume. Volume lift is carried in the alert
payload as displayed context only.

---

## Held-out results (sealed run, 3 disclosed reads)

Source: `reports/heldout_test/results.json` · `reports/heldout_test/report.md`

### Spike / event level — the headline

| Metric | Value |
|---|---:|
| Event recall | **1.000** (3 of 3 test spike events detected) |
| Event precision | **1.000** |
| False alerts | **0** |
| **False alerts inside benign surge windows** | **0** |
| Median detection delay | 30.0 min |
| P90 detection delay | 30.0 min |

The two held-out benign surges (`TST_B1`, `TST_B2`) produced zero alerts. That is the
false-positive-pressure result the whole design targets, and it is measured, not staged.

### Transaction level

| Metric | Value |
|---|---:|
| PR-AUC (calibrated) | 0.9485 |
| ROC-AUC (calibrated) | 0.9969 |
| Recall | 0.9780 |
| Precision | 0.6209 |
| F1 | 0.7596 |
| True positives / False positives | 267 / 163 |
| False negatives / True negatives | 6 / 5564 |
| Operating threshold (raw XGBoost probability) | 0.3715 |

**Honest caveat, stated plainly:** held-out precision (0.62) fell well below the ≥0.90 precision
floor that was selected on validation, while recall rose to 0.978. The threshold was frozen before
this run and was not re-tuned afterwards. This is the degradation the project's own risk register
predicted: the test spike families (`low_amount_microburst`, `trusted_device_takeover_like`,
`cross_channel_burst`) are labelled `difficulty=hard` and are deliberately designed to defeat
reliance on the signatures present in train and validation. Ranking quality held up
(PR-AUC 0.9485), so the loss is in threshold placement rather than in the model's ordering.

### Business outcome

| Metric | INR |
|---|---:|
| Fraud value captured | 1,153,387.28 |
| Fraud value missed | 18,171.85 |
| False-positive cost | 50,224.68 |
| Legitimate value disrupted | 545,282.60 |
| Analyst review cost *(assumption)* | 750.00 |
| Customer friction cost *(assumption)* | 6,520.00 |
| **Net risk benefit** | **1,095,892.60** |

Per-transaction fraud loss and false-positive cost come from the dataset. Analyst review
(₹250/incident), customer friction (₹40/stepped-up customer), and detection delay (₹500/hour) are
**our documented assumptions**, listed in `reports/COST_ASSUMPTIONS.md` and varied in the
sensitivity table in `reports/heldout_test/report.md`.

### Safety and policy

| Property | Result |
|---|---|
| Deterministic safety-policy violations | **0** |
| Actions authorized by a language model | **0** |
| Financial values produced by a language model | **0** |
| Automatic policy promotion paths in code | **0** |
| Candidate policy status | shadow only, never operative |

Live agent-narrative metrics (grounding accuracy, verification rejection rate, recommendation
acceptance and override rates) were **not measured** in this run: they need the running stack with an
LLM credential, which was unavailable at freeze time. The report says so rather than estimating.

---

## Architecture

```
                    ┌──────────────────── Redpanda topics ────────────────────┐
                    │ transactions · fraud_scores · spike_alerts · incidents  │
                    │ agent_events · analyst_actions · responses · outcomes   │
                    │ rewards · alerts                                        │
                    └───────┬─────────────────────────────────────┬───────────┘
                            │                                     │
   stream_transactions.py ──┤                                     ├── EventConsumer
   (virtual clock replay)   │                                     │   (transactions, outcomes)
                            ▼                                     ▼
              ┌─────────────────────────┐            ┌──────────────────────────┐
              │ TransactionService      │            │ EvaluationService        │
              │  build_features (one    │            │  reward from real cost   │
              │  contract, train+serve) │            │  fields, idempotent      │
              │  XGBoost + isotonic     │            └──────────┬───────────────┘
              │  calibration            │                       │
              │  ↓ ResilientFraudScorer │                       ▼
              │  (IsolationForest, then │            ┌──────────────────────────┐
              │   deterministic rules)  │            │ Offline learning         │
              └───────────┬─────────────┘            │  RandomForest reward     │
                          │                          │  LinUCB candidate policy │
                          ▼                          │  (no live exploration)   │
              ┌─────────────────────────┐            └──────────┬───────────────┘
              │ RiskDensitySpikeDetector│                       │ shadow only
              │  120-min window /       │                       ▼
              │  15-min slide           │            ┌──────────────────────────┐
              │  support gate → EWMA    │            │ PolicyService            │
              │  baseline → lift →      │            │  registry-backed serving │
              │  Poisson significance → │            │  checksum-verified       │
              │  2-step confirmation    │            │  artifacts, admin-only   │
              │  promo raises the bar,  │            │  promote / rollback      │
              │  never vetoes           │            └──────────────────────────┘
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │ Deterministic segment   │  greedy, depth ≤ 3, significance-filtered
              │ discovery (no LLM)      │
              └───────────┬─────────────┘
                          ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │ LangGraph investigation (Postgres checkpointer, genuine interrupt)        │
   │                                                                          │
   │  observe → retrieve_evidence → analyze_spike → discover_segment →         │
   │  investigate_root_cause → verify_evidence → estimate_impact →             │
   │  evaluate_responses → policy_gate → [HUMAN REVIEW INTERRUPT] →            │
   │  finalize → capture_outcome                                              │
   │                                                                          │
   │  Gemini tiers: 3.5-flash-lite → 3.1-flash-lite → gemma-4-31b-it →         │
   │                deterministic template (always yields a usable incident)   │
   │  Structured output only. Every claim carries resolvable evidence IDs.     │
   └──────────────────────────────┬───────────────────────────────────────────┘
                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │ Deterministic authority (pure Python, no I/O, no model in the path)      │
   │  PolicyEngine  ·  evidence grounding floor  ·  role permissions          │
   │  → allow | require_approval | deny                                       │
   │ Human analyst: Approve / Modify / Reject / Escalate → append-only audit  │
   │ Outcomes and rewards published through a transactional outbox            │
   └──────────────────────────────────────────────────────────────────────────┘
```

### Stack

| Layer | Technology |
|---|---|
| Frontend | HTML + CSS + vanilla JavaScript (no framework, no build step) |
| Backend | FastAPI |
| Primary fraud ML | XGBoost + isotonic calibration |
| Fallback risk signal | Isolation Forest, then deterministic rules |
| Reward model | RandomForestRegressor |
| Response policy | LinUCB (offline only; Thompson sampling logged as a benchmark) |
| Agents | LangGraph + LangChain, Gemini via a structured-output gateway |
| Database | PostgreSQL 16 + SQLAlchemy + Alembic |
| Cache | Redis 7 (bounded in-process LRU fallback) |
| Streaming | Redpanda (Kafka API) via aiokafka |
| Experiments | MLflow |
| Metrics / dashboards / alerts | Prometheus + Grafana |
| Tracing | LangSmith |
| Packaging | Docker Compose (infrastructure) + venv (app) |

---

## Seven properties this build treats as "production grade"

1. **Typed boundaries** — Pydantic schemas on every API edge and every model output.
2. **Graceful degradation** — every dependency has a defined, *visible* failure behaviour. All eight
   documented failure cases (fraud model, LLM, Redis, Redpanda, Postgres, unsupported claim, policy
   violation, underperforming candidate) plus a corrupt response-policy artifact are exercised by
   `scripts/failure_drills.py`: **9/9 pass** (`reports/FAILURE_DRILLS.md`).
3. **Idempotency** — duplicate transactions, duplicate events, replayed outcomes, and repeated
   reward calculation cannot double-count.
4. **Deterministic authority** — models recommend; a pure Python policy function authorizes.
5. **Auditability** — detector output, evidence, claims, verification verdicts, policy decisions,
   human actions, and outcomes are all retained and reconstructable.
6. **Observability** — one Prometheus catalog (`backend/app/monitoring/prometheus.py`), four
   provisioned Grafana dashboards, five alert classes, structured logs with correlation IDs.
7. **Honest measurement** — the test holdout is reachable only through a guarded loader, and
   `evaluation/heldout_report.py` is its only caller.

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements/all.txt          # all versions open, no pyproject.toml

Copy-Item .env.example .env                  # then set real secrets
docker compose up -d                         # postgres, redis, redpanda, mlflow, prometheus, grafana
python -m alembic -c backend\alembic.ini upgrade head
python scripts\seed_database.py

python scripts\run_api.py --port 8000       # use this, not `uvicorn` directly, on Windows
python scripts\stream_transactions.py --split validation --speed 300
```

> **Windows event loop.** `scripts/run_api.py` installs a selector event loop before the loop is
> created. psycopg's async driver refuses Windows' default `ProactorEventLoop`, so starting with
> `uvicorn backend.app.main:app` leaves durable Postgres checkpointing unavailable — the app still
> starts and says so in the header strip, but Approve and Modify stay disabled because they require
> a durable checkpoint. On Linux and macOS either command works.
>
> **Connection refused.** An `outbox_cycle_failed` warning with WinError 1225 identifies the
> Postgres claim/commit path, before Redpanda publication. Ensure Docker Desktop is running, execute
> `docker compose up -d`, and check readiness with `docker compose ps`. Pending outbox rows remain
> durable while the dispatcher backs off (up to `OUTBOX_CYCLE_RETRY_MAX_SECONDS`) and it emits
> `outbox_cycle_recovered` after Postgres accepts a cycle.

Open `http://localhost:8000/` for the command center, `http://localhost:3000/` for Grafana,
`http://localhost:5000/` for MLflow.

### Offline pipeline

```powershell
python scripts\run_evaluation.py --check      # leakage lint + metric contracts
python scripts\run_evaluation.py --phase2     # validation + train-tail dev-test benchmark
python training\train_fraud_model.py
python training\train_reward_model.py
python training\train_bandit.py
python evaluation\evaluate_policy.py
python evaluation\heldout_report.py           # sealed; run once at freeze
python scripts\failure_drills.py
python scripts\check_frontend.py
```

---

## Interface

Dark enterprise command center, six views, all live data:

- **Landing** — the risk-density thesis, animated preview, sign-in.
- **Risk Dashboard** — live ticker, risk-density trend with incident and promotion shading (volume
  drawn alongside as context), incident cards contrasting density lift against volume lift, alert
  center, exposure/cost summary, drift.
- **Incidents** — filterable queue; every row shows density lift beside volume lift.
- **Investigation** — agent timeline with per-stage model, prompt version, and evidence hash; claim
  verification badges; grounding gauge; ranked segments; evidence store; counterfactual response
  cards with the action-effect assumptions printed beside every number; policy gate; HITL panel;
  audit trail.
- **Models & Policies** — production vs candidate comparison with deltas, promotion gate checks,
  admin-only promote/rollback, policy version lifecycle, model registry, drift.
- **Held-out Report** — renders `results.json` directly; if the sealed run has not happened, it says
  so instead of showing numbers.

A persistent header strip shows every dependency's health. Motion respects
`prefers-reduced-motion`.

---

## Documented deviations from the plan

| Plan item | What was built | Why |
|---|---|---|
| Charts via a vendored Chart.js UMD bundle | ~250-line dependency-free canvas renderer (`frontend/js/charts.js`) | A third-party bundle could not be honestly vendored offline in this environment; the needed surface (animated area/line with shaded event windows, sparklines, bars) is small, works offline, and is DPI-aware |
| Landing video block | `<video>` used when `frontend/assets/videos/demo.mp4` exists, otherwise an animated SVG preview | No recorded clip is committed; the fallback is honest rather than a broken element |
| "Held-out labels read exactly once" | Read three times, each disclosed with its reason in `reports/heldout_test/ACCESS_LOG.md` | Reads 2 and 3 fixed defects in the *report generator*, not in any model, threshold, detector parameter, or policy. The model checksum and every transaction, event, and business metric were identical across all three reads. `--render-only` now regenerates the report without touching sealed labels, and the read counter refuses to run rather than understate an unparsed log |
| Drift on `p_fraud` **and key feature distributions** | PSI on the calibrated score only | Only the calibrated score is observable from the live scores table. Advertising feature-level drift that can never be measured would read as "measured and stable" when it is really "never observed" |
| `evaluation/evaluate_agents.py` | Agent metrics folded into `heldout_report.py`, with live narrative metrics explicitly recorded as not measured | Avoids a thin module and avoids fabricating metrics that need an unavailable credential |

---

## Repository map

```
backend/app/
  agents/         LangGraph graph, nodes, prompts, tools, state
  api/            routes (health, transactions, incidents, decisions, feedback, metrics, models), websocket
  cache/          Redis client, keys, cache service with LRU fallback
  core/           config, logging, runtime (DegradationState, AppError, VirtualClock), security, constants
  db/             models, repositories, session
  hitl/           review service, approval rules, feedback service
  llm/            structured gateway, tier routing
  ml/             fraud (features, predictor), spike_detection, reward, policy, artifacts
  monitoring/     Prometheus catalog, PSI drift monitor
  safety/         policy engine, evidence grounding, permissions, escalation, metrics
  services/       transaction, incident, investigation, evaluation, policy
  streaming/      producer, consumer, outbox, topics
evaluation/       dataio (guarded), metrics, replay, per-domain evaluators, heldout_report
training/         fraud model, reward model, bandit
frontend/         index.html, pages/, css/, js/, assets/
monitoring/       prometheus config, alerts, Grafana provisioning + dashboards
scripts/          seed, stream, inject spike, reset demo, evaluation, failure drills, frontend check
tests/            14 files, exactly as budgeted
reports/          protocol, cost assumptions, EDA, metrics, failure drills, demo runbook, heldout_test/
```

---

## Documentation

| Document | Contents |
|---|---|
| `reports/EVALUATION_PROTOCOL.md` | Fit / calibrate / threshold / freeze protocol |
| `reports/COST_ASSUMPTIONS.md` | Which figures are measured and which are our assumptions |
| `reports/heldout_test/report.md` | The sealed evaluation, in full, with sensitivity |
| `reports/heldout_test/CAVEATS.md` | Caveats found after the run: precision shortfall, why the policy comparison is not a serving forecast |
| `reports/heldout_test/ACCESS_LOG.md` | Every read of the sealed labels, with reasons |
| `reports/FAILURE_DRILLS.md` | The degradation matrix, drilled |
| `reports/DEMO_RUNBOOK.md` | Nine scenes plus the benign-surge beat, under 6 minutes |
| `reports/EDA_FINDINGS.md` | The volume-is-a-decoy analysis |
| `reports/metrics/phase2_benchmark.md` | Validation and dev-test detector benchmark |
| `reports/metrics/phase6_shadow_policy.md` | Production vs candidate policy comparison |
| `reports/env_snapshot.txt` | `pip freeze` reproducibility record |

---

## Known limitations

- Held-out transaction precision (0.62) is below the validation floor (0.90). Discussed above; not
  retro-fitted.
- Live agent-narrative metrics and live Postgres/Redis/Redpanda/Gemini/LangSmith drills were not
  executed in this environment: the Docker Desktop Linux engine was unavailable and no model
  credential was configured. Infrastructure configuration is validated statically
  (`docker compose config`, Alembic offline SQL through head) and every degradation path is drilled
  in-process instead.
- Authentication is demo-grade: short-lived JWTs and seeded operator accounts. Suitable for the
  demo, not for production identity.
- Event delivery is at-least-once. The outbox gives durable, retryable publication with stable event
  IDs; consumers deduplicate by `event_id`.
- The WebSocket accepts its bearer token as a query parameter and checks signature but not role, so
  it is read-only telemetry at demo grade. Every state-changing route enforces roles.
- The held-out policy comparison scores both rankings using offline-training context semantics, so
  it is a like-for-like ranking comparison, not a serving-time forecast. Nothing is promoted from it.
- Accessibility is built to AA contrast and keyboard operability. Full WCAG conformance needs manual
  assistive-technology testing and expert review, which was out of scope.

## Author
Sujato Dutta - AI Engineer | Researcher