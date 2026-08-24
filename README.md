# Multi-Agentic AI Fraud Spike Detector

A fraud-operations command center that detects unusual increases in **fraud risk**,
investigates them with AI agents, and keeps a human analyst in control of every
response.

The system focuses on **risk density** instead of transaction volume. This matters
because a busy shopping day and a fraud attack can have similar traffic levels,
but very different fraud rates.

## What the project does

- Scores transactions with a calibrated fraud model.
- Detects fraud spikes without using volume as a trigger.
- Finds the customer or transaction segments driving each spike.
- Runs an evidence-based, multi-agent investigation.
- Checks every recommendation with deterministic safety rules.
- Requires human approval before an action is finalized.
- Records decisions, outcomes, rewards, and audit history.
- Shows live operations, models, incidents, and reports in a web dashboard.

## System architecture

1. Transactions arrive through the API or Redpanda.
2. The fraud model assigns a calibrated risk score.
3. The spike detector watches the fraud-risk density over time.
4. A deterministic segment search identifies what is driving the spike.
5. Investigation agents collect evidence and explain likely causes.
6. A verification agent rejects unsupported claims.
7. The policy engine checks permissions, evidence quality, and safety rules.
8. A human analyst approves, modifies, rejects, or escalates the recommendation.
9. The system stores the decision and publishes outcomes through a durable outbox.

```mermaid
flowchart TB
    classDef user fill:#E8F1FF,stroke:#2563EB,color:#0F172A,stroke-width:1.5px
    classDef service fill:#E6FFFB,stroke:#0F766E,color:#0F172A,stroke-width:1.5px
    classDef model fill:#F3E8FF,stroke:#7E22CE,color:#0F172A,stroke-width:1.5px
    classDef safety fill:#FFF1F2,stroke:#BE123C,color:#0F172A,stroke-width:1.5px
    classDef data fill:#FFFBEB,stroke:#B45309,color:#0F172A,stroke-width:1.5px
    classDef stream fill:#ECFDF5,stroke:#15803D,color:#0F172A,stroke-width:1.5px
    classDef observe fill:#F1F5F9,stroke:#475569,color:#0F172A,stroke-width:1.5px

    subgraph EXPERIENCE["Experience and ingress"]
        direction LR
        Analyst["Analyst / Admin"]
        Dashboard["Command Center<br/>REST polling + WebSocket"]
        Replay["Transaction replay<br/>or upstream producer"]
        Analyst --> Dashboard
    end

    subgraph APPLICATION["FastAPI application"]
        direction LR
        API["FastAPI<br/>Auth · REST routes · static frontend"]
        WS["WebSocket Hub<br/>Live best-effort updates"]
        TxService["Transaction Service<br/>Idempotent ingestion owner"]
        IncidentService["Incident Service"]
        InvestigationService["Investigation Service"]
        ReviewService["HITL Review Service"]
        FeedbackService["Outcome / Feedback Service"]
        EvaluationService["Reward Evaluation Service"]
        PolicyService["Policy Registry Service<br/>Admin promote / rollback"]
    end

    Dashboard -->|authenticated REST| API
    WS -->|live updates| Dashboard
    Replay -->|service-token REST| API
    API --> TxService
    API --> ReviewService
    API --> FeedbackService
    API --> PolicyService

    subgraph DETECTION["Fraud scoring and spike detection"]
        direction LR
        Features["Shared feature contract<br/>Train / serve parity"]
        Scorer["Resilient Fraud Scorer"]
        Primary["XGBoost + isotonic calibration"]
        Anomaly["Isolation Forest fallback"]
        Rules["Deterministic rules fallback"]
        Windows["Event-time sliding windows<br/>120 min / 15 min slide"]
        Detector["Risk-Density Spike Detector<br/>EWMA · lift · Poisson · persistence"]
        Segments["Deterministic segment discovery<br/>Depth ≤ 3 · significance filtered"]

        Features --> Scorer --> Primary --> Windows
        Primary -. unavailable .-> Anomaly
        Anomaly -. unavailable .-> Rules
        Anomaly --> Windows
        Rules --> Windows
        Windows --> Detector --> Segments
    end

    TxService --> Features
    Segments --> IncidentService
    IncidentService -->|schedule investigation| InvestigationService
    IncidentService -->|alert + incident update| WS

    subgraph INVESTIGATION["Evidence-grounded investigation and authority"]
        direction LR
        AgentGraph["LangGraph<br/>Observe · Retrieve · Analyze · Segment<br/>Root cause · Verify · Impact · Respond"]
        Gateway["Structured LLM Gateway<br/>Schema validation · retry · circuit breaker · cache"]
        Gemini["Gemini tier routing<br/>3.5 Flash Lite → 3.1 Flash Lite → Gemma 4"]
        Template["Deterministic template fallback"]
        Grounding["Python evidence grounding<br/>Unsupported claims fail closed"]
        PolicyEngine["Deterministic Policy Engine<br/>Allow · approval · deny"]
        HumanGate["Human review interrupt<br/>Approve · Modify · Reject · Escalate"]

        AgentGraph -->|typed requests| Gateway --> Gemini
        Gemini -->|structured output| AgentGraph
        Gateway -. tiers exhausted .-> Template --> AgentGraph
        AgentGraph --> Grounding --> PolicyEngine --> HumanGate
    end

    InvestigationService --> AgentGraph
    HumanGate --> ReviewService
    ReviewService -->|durable resume| AgentGraph
    ReviewService -->|decision update| WS
    FeedbackService -->|audit update| WS

    subgraph DATA["State, cache, and durable checkpoints"]
        direction LR
        Postgres[("PostgreSQL<br/>System of record")]
        Records["Operational records<br/>Txns · scores · incidents · evidence"]
        Governance["Governance records<br/>Decisions · audit · rewards · policies · outbox"]
        Checkpointer["LangGraph checkpointer<br/>Postgres durable / in-memory degraded"]
        CacheService["Cache Service"]
        Redis[("Redis<br/>Claims · predictions · LLM cache · snapshots")]
        LocalLRU["Bounded local TTL-LRU fallback"]

        Postgres --- Records
        Postgres --- Governance
        Checkpointer --> Postgres
        CacheService --> Redis
        Redis -. unavailable .-> LocalLRU
    end

    TxService -->|commit before detection| Postgres
    IncidentService --> Postgres
    InvestigationService -->|evidence + provenance| Postgres
    ReviewService -->|decision + audit + outbox| Postgres
    FeedbackService -->|outcome + audit + outbox| Postgres
    EvaluationService -->|reward + memory + outbox| Postgres
    AgentGraph --> Checkpointer
    TxService --> CacheService
    Gateway --> CacheService
    Postgres -->|similar incident memories| InvestigationService

    subgraph STREAMING["Redpanda event backbone"]
        direction LR
        Producer["Event Producer<br/>Stable event envelopes + trace IDs"]
        Redpanda[("Redpanda<br/>Kafka-compatible broker")]
        DetectionTopics["Detection topics<br/>transactions · fraud_scores · spike_alerts · incidents"]
        DecisionTopics["Decision topics<br/>analyst_actions · outcomes · rewards"]
        ReservedTopics["Reserved topic contracts<br/>agent_events · responses · alerts"]
        Consumer["Event Consumer<br/>Active handlers: transactions + outcomes"]
        Outbox["Outbox Dispatcher<br/>Claim · lease · retry · deduplicated recovery"]

        Producer --> Redpanda --> Consumer
        Redpanda --- DetectionTopics
        Redpanda --- DecisionTopics
        Redpanda --- ReservedTopics
    end

    Replay -->|Kafka transaction event| Redpanda
    Consumer -->|transactions| TxService
    Consumer -->|outcomes| EvaluationService
    TxService -->|direct fraud score event| Producer
    IncidentService -->|direct spike + incident events| Producer
    Postgres -->|pending analyst action / outcome / reward| Outbox
    Outbox --> Producer

    subgraph LEARNING["Offline learning and controlled model lifecycle"]
        direction LR
        Datasets["Train · validation · sealed holdout"]
        Trainers["Offline trainers<br/>XGBoost · Isolation Forest · Reward RF · LinUCB"]
        Artifacts["Checksum-verified<br/>model and policy artifacts"]
        MLflow["MLflow<br/>Experiments and artifacts"]
        Resolver["Runtime Policy Resolver<br/>Production + shadow candidate"]

        Datasets --> Trainers --> Artifacts
        Trainers --> MLflow
        Artifacts --> Resolver
    end

    Artifacts --> Scorer
    Resolver --> AgentGraph
    PolicyService -->|manual gated lifecycle| Postgres
    Postgres -->|active policy registry| Resolver

    subgraph OBSERVABILITY["Observability and operations"]
        direction LR
        Drift["PSI Drift Monitor<br/>Advisory only"]
        Metrics["Prometheus metric catalog<br/>HTTP · fraud · agents · HITL · ML · stream"]
        Prometheus["Prometheus<br/>15-second scrape"]
        AlertRules["Alert rules<br/>Spike · drift · agent · policy · dependency"]
        Grafana["Grafana<br/>4 provisioned dashboards"]
        Logs["Structured JSON logs<br/>Correlation / trace IDs"]
        LangSmith["LangSmith tracing<br/>Conditional investigation traces"]

        Prometheus -->|scrapes /metrics| Metrics
        Prometheus --> AlertRules
        Grafana -->|queries| Prometheus
    end

    TxService --> Drift
    Drift -. metrics .-> Metrics
    API -. HTTP + dependency metrics .-> Metrics
    AgentGraph -. agent + LLM + HITL metrics .-> Metrics
    Producer -. streaming metrics .-> Metrics
    API -. JSON stdout .-> Logs
    InvestigationService -. when enabled .-> LangSmith

    class Analyst,Dashboard,Replay user
    class API,WS,TxService,IncidentService,InvestigationService service
    class ReviewService,FeedbackService,EvaluationService,PolicyService service
    class Features,Scorer,Primary,Anomaly,Rules,Windows,Detector,Segments model
    class AgentGraph,Gateway,Gemini,Template,Resolver,Trainers,Artifacts model
    class Grounding,PolicyEngine,HumanGate safety
    class Postgres,Records,Governance,Checkpointer,CacheService,Redis,LocalLRU data
    class Producer,Redpanda,DetectionTopics,DecisionTopics,ReservedTopics,Consumer,Outbox stream
    class Drift,Metrics,Prometheus,AlertRules,Grafana,Logs,LangSmith,MLflow observe
```

Solid arrows show implemented runtime or data flows. Dotted arrows show fallbacks,
telemetry, or optional integrations. Language models provide structured analysis;
only deterministic policy checks and a human reviewer can authorize a response.

## Quick start

### 1. Install the application

Run these commands from the project root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements/all.txt
```

### 2. Configure the environment

```powershell
Copy-Item .env.example .env
```

Open `.env` and replace the values in the **REQUIRED** section. Optional
integrations such as Gemini and LangSmith can remain blank.

### 3. Start the infrastructure

Make sure Docker Desktop is running, then execute:

```powershell
docker compose up -d
docker compose ps
```

This starts PostgreSQL, Redis, Redpanda, MLflow, Prometheus, and Grafana.

### 4. Prepare the database

```powershell
python -m alembic -c backend\alembic.ini upgrade head
python scripts\seed_database.py
```

### 5. Start the API and dashboard

```powershell
python scripts\run_api.py --port 8000
```

On Windows, use `scripts/run_api.py` instead of starting Uvicorn directly. The
script installs the event loop required by psycopg.

### 6. Stream sample transactions

Open another PowerShell terminal, activate the virtual environment, and run:

```powershell
python scripts\stream_transactions.py --split validation --speed 300
```

## Service URLs

| Service | URL |
|---|---|
| Command center | http://localhost:8000/ |
| API documentation | http://localhost:8000/docs |
| Grafana | http://localhost:3000/ |
| MLflow | http://localhost:5000/ |
| Prometheus | http://localhost:9090/ |

## Common startup problem

If you see `outbox_cycle_failed` with `WinError 1225`, PostgreSQL is not
accepting connections. Start Docker Desktop and run:

```powershell
docker compose up -d
docker compose ps
```

The outbox does not delete pending events. It retries with a capped backoff and
logs `outbox_cycle_recovered` when PostgreSQL becomes available again.

## Why risk density matters

The dataset contains both fraud spikes and legitimate traffic surges:

| Traffic type | Volume compared with normal | Fraud rate |
|---|---:|---:|
| Normal traffic | About 1.0× | 1.69%–1.83% |
| Fraud spikes | 2.3×–2.9× | 13.7%–22.3% |
| Legitimate surges | 2.2×–2.6× | 0.8%–2.3% |

Fraud spikes and legitimate surges can have almost the same volume. A
volume-based alert would therefore generate false alarms during festivals,
flash sales, or salary days.

This detector triggers on calibrated fraud probability. Volume is shown in the
dashboard for context, but it cannot trigger an incident.

## Dashboard

The frontend uses HTML, CSS, and vanilla JavaScript. It has no build step.

| View | Purpose |
|---|---|
| Landing | Sign-in and an animated overview of the system |
| Risk Dashboard | Live scores, risk trends, incidents, exposure, alerts, and drift |
| Incidents | Searchable and filterable incident queue |
| Investigation | Agent timeline, evidence, segments, recommendations, policy checks, and human review |
| Models & Policies | Production and candidate comparison, drift, promotion checks, and rollback controls |
| Held-out Report | Displays the sealed evaluation directly from `results.json` |

The header shows the health of each dependency. Animations respect the operating system's reduced-motion preference.

## Technology stack

| Area | Technology |
|---|---|
| Frontend | HTML, CSS, vanilla JavaScript |
| API | FastAPI |
| Fraud model | XGBoost with isotonic calibration |
| Fraud-model fallbacks | Isolation Forest, then deterministic rules |
| Agent workflow | LangGraph and LangChain |
| LLM provider | Gemini with structured outputs |
| Database | PostgreSQL, SQLAlchemy, Alembic |
| Cache | Redis with an in-process LRU fallback |
| Streaming | Redpanda through the Kafka API |
| Reward model | Random Forest regressor |
| Response policy | LinUCB trained offline |
| Experiment tracking | MLflow |
| Monitoring | Prometheus and Grafana |
| Tracing | LangSmith |

## Reliability and safety

The system is designed to fail visibly and safely.

| Failure | Behavior |
|---|---|
| Primary fraud model unavailable | Uses Isolation Forest, then deterministic rules |
| Gemini unavailable | Tries lower-cost model tiers, then uses a deterministic template |
| Redis unavailable | Uses a bounded in-process cache |
| Redpanda unavailable | Keeps unpublished events in the PostgreSQL outbox |
| PostgreSQL unavailable | Reports degradation and retries database-dependent work |
| Unsupported agent claim | Rejects the claim during evidence verification |
| Unsafe recommendation | Blocks or escalates it through deterministic policy rules |
| Weak candidate policy | Keeps it in shadow mode and refuses promotion |

Important safety rules:

- Language models can recommend actions, but they cannot authorize them.
- Language models do not calculate financial impact.
- Policy promotion is admin-only and never automatic.
- Duplicate transactions and events are handled idempotently.
- Every important decision has an audit trail.
- Delivery is at least once; consumers deduplicate events using `event_id`.

All nine failure drills pass. See [Failure drills](reports/FAILURE_DRILLS.md) for details.

## Held-out evaluation

The values below come from `reports/heldout_test/results.json`. The dashboard and
README do not invent or hardcode evaluation results.

### Spike detection

| Metric | Result |
|---|---:|
| Event recall | **1.000** — 3 of 3 fraud spikes detected |
| Event precision | **1.000** |
| False alerts | **0** |
| False alerts during legitimate surges | **0** |
| Median detection delay | 30 minutes |
| P90 detection delay | 30 minutes |

### Transaction classification

| Metric | Result |
|---|---:|
| PR-AUC | 0.9485 |
| ROC-AUC | 0.9969 |
| Recall | 0.9780 |
| Precision | 0.6209 |
| F1 | 0.7596 |
| True positives | 267 |
| False positives | 163 |
| False negatives | 6 |
| True negatives | 5,564 |
| Frozen threshold | 0.3715 |

### Business result

| Metric | Result |
|---|---:|
| Fraud value captured | ₹1,153,387.28 |
| Fraud value missed | ₹18,171.85 |
| False-positive cost | ₹50,224.68 |
| Legitimate value disrupted | ₹545,282.60 |
| Analyst review cost | ₹750.00 |
| Customer friction cost | ₹6,520.00 |
| **Net risk benefit** | **₹1,095,892.60** |

Analyst review, customer friction, and delay costs are assumptions rather than
measured transaction fields. They are documented in
[Cost assumptions](reports/COST_ASSUMPTIONS.md).

### Important evaluation caveat

Held-out precision was **0.62**, below the validation target of **0.90**. Recall
increased to **0.978**. The threshold was frozen before the held-out run and was
not adjusted afterwards.

The model still ranked transactions well, with a PR-AUC of **0.9485**, but the
frozen threshold did not transfer cleanly to the harder held-out fraud patterns.
See the [full held-out report](reports/heldout_test/report.md) and
[held-out caveats](reports/heldout_test/CAVEATS.md).

## Evaluation integrity

- Held-out labels are available only through a guarded loader.
- The labels were accessed three times. Every access is recorded in the
  [access log](reports/heldout_test/ACCESS_LOG.md).
- The second and third reads fixed report-generation defects only.
- No model, threshold, detector setting, or policy changed after the first read.
- Transaction, event, and business metrics were identical across all three reads.
- Live agent-narrative metrics were marked as not measured because an LLM
  credential was unavailable at freeze time.

## Useful commands

### Offline training and evaluation

```powershell
python scripts\run_evaluation.py --check
python scripts\run_evaluation.py --phase2
python training\train_fraud_model.py
python training\train_reward_model.py
python training\train_bandit.py
python evaluation\evaluate_policy.py
python evaluation\heldout_report.py
```

The held-out evaluation is sealed. Do not rerun it casually after the project is frozen.

### Failure drills and frontend checks

```powershell
python scripts\failure_drills.py
python scripts\check_frontend.py
```

### Project validation

```powershell
python -m pytest tests -q
python -m ruff check backend training evaluation scripts tests
python -m compileall -q backend training evaluation scripts tests
docker compose config --quiet
```

## Project structure

```text
backend/app/     API, services, agents, ML, safety, database, and streaming
frontend/        Dashboard pages, styles, JavaScript, and assets
training/        Fraud, reward, and policy training scripts
evaluation/      Guarded data loading and evaluation code
monitoring/      Prometheus, Grafana, dashboards, and alerts
infrastructure/  Policy and action-effect configuration
scripts/         Setup, replay, evaluation, drills, and validation tools
tests/           Automated test suite
reports/         Evaluation, assumptions, drills, caveats, and demo guides
models/          Saved model and policy artifacts
data/            Dataset files and generated data
```

## Reports and guides

- [Evaluation protocol](reports/EVALUATION_PROTOCOL.md) — training, calibration,
  threshold selection, and freeze process.
- [Cost assumptions](reports/COST_ASSUMPTIONS.md) — measured values and business
  assumptions.
- [Held-out report](reports/heldout_test/report.md) — full sealed evaluation and
  sensitivity analysis.
- [Held-out caveats](reports/heldout_test/CAVEATS.md) — precision shortfall and
  policy-comparison limits.
- [Held-out access log](reports/heldout_test/ACCESS_LOG.md) — every access to the
  sealed labels.
- [Failure drills](reports/FAILURE_DRILLS.md) — dependency and safety failure tests.
- [Demo runbook](reports/DEMO_RUNBOOK.md) — guided product demonstration.
- [EDA findings](reports/EDA_FINDINGS.md) — why transaction volume is a poor
  fraud-spike signal.
- [Phase 2 benchmark](reports/metrics/phase2_benchmark.md) — validation and
  development benchmark.
- [Policy comparison](reports/metrics/phase6_shadow_policy.md) — production and
  candidate policy comparison.

## Implementation notes

A few planned items were adjusted during implementation:

- Charts use a small local canvas renderer instead of Chart.js. This keeps the
  frontend offline-ready without a vendored third-party bundle.
- The landing page uses a demo video when one is present and an animated SVG
  preview otherwise.
- Drift monitoring measures the calibrated fraud score. Feature-level drift is
  not reported because those distributions are not stored in the live score table.
- Live agent metrics are marked as not measured instead of being estimated.

## Known limitations

- Held-out transaction precision is below the validation target.
- Live dependency and LLM drills were not run against real services during the
  frozen evaluation because Docker Desktop and an LLM credential were unavailable.
  The same failure paths were tested in-process.
- Authentication uses demo accounts and short-lived JWTs. It is not a production
  identity system.
- The read-only WebSocket receives its token through the query string. It validates
  the signature but not the user's role.
- The held-out policy comparison is an offline ranking comparison, not a promise
  of serving-time performance.
- Accessibility includes keyboard support, reduced motion, and AA-oriented
  contrast. Full WCAG conformance still needs manual assistive-technology testing.

## Author

**Sujato Dutta** — AI Engineer and Researcher
