# Demo Runbook

Target runtime: under 6 minutes. Every number shown on screen is read from the running
system or from `reports/heldout_test/results.json`. Nothing is staged.

## 0. Preparation (before the recording starts)

```powershell
docker compose up -d                                  # postgres, redis, redpanda, mlflow, prometheus, grafana
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe scripts\reset_demo.py --yes
.\.venv\Scripts\python.exe scripts\seed_database.py
.\.venv\Scripts\python.exe scripts\run_api.py --port 8000
```

Start the API through `scripts/run_api.py`, not `uvicorn` directly. On Windows it installs the
selector event loop that psycopg requires; without it durable Postgres checkpointing is unavailable
and the HITL Approve/Modify buttons stay disabled, which would break scene 6. The header strip shows
`Checkpoint degraded` when this happens, so the failure is visible rather than silent.

Then, in a second terminal, warm the detector baseline before going live:

```powershell
.\.venv\Scripts\python.exe scripts\stream_transactions.py --split validation --speed 600 --to 2026-05-29T00:00:00
```

Open `http://localhost:8000/` for the command center and `http://localhost:3000/` for Grafana.

Virtual clock: `--speed 300` means 1 real second is 5 dataset minutes, so a 10-hour event
plays in about 2 minutes and a 90-minute detection delay appears in about 18 seconds.

## Scene map

| # | Scene | Trigger | What the audience sees |
|--:|---|---|---|
| 1 | Healthy system | open `/`, sign in | Animated landing with the risk-density thesis, then the command center: live ticker, normal risk trend, all dependencies green, model versions registered |
| 2 | Spike begins | `scripts\inject_demo_spike.py --event VAL_S1` | Risk-density line climbs while volume stays ordinary; alert toast fires; incident card appears with density lift, volume lift, and exposure |
| 3 | Segment discovery | automatic | Investigation view shows ranked segments with support, lift, and per-condition contribution — computed deterministically, no model involved |
| 4 | Agent investigation | automatic | Agent timeline fills stage by stage, each with its model, prompt version, and evidence hash; claims carry verification badges; grounding gauge animates to its measured value |
| 5 | Response comparison | automatic | Counterfactual response cards ranked by the production policy, with the action-effects assumptions printed beside every number |
| 6 | Human decision | analyst clicks Approve / Modify / Reject / Escalate | Policy gate shows `require_approval`; the decision writes to the audit trail live; role-permitted actions only |
| 7 | Learning from memory | replay reaches a later similar event | Recommendation context includes the prior incident's outcome; memory is advisory and cannot authorize |
| 8 | Shadow self-improvement | open Models & Policies | Production vs candidate comparison with deltas; promotion gate chips show which checks fail; promote button is disabled unless signed in as admin |
| 9 | Failure drill | `scripts\failure_drills.py --only fraud_model_missing` | Fraud model marked degraded in the header strip, conservative rule scoring continues, detection keeps running |
| ★ | Bonus: benign surge | replay reaches `VAL_B1` (or `TST_B1` on the test split) | Volume rises ~2.2× with **no alert**. This is the false-positive-cost result, and it is measured: the held-out run recorded 0 false alerts inside benign surge windows |

## Scene 9 alternatives

Each drill is scripted and reversible:

```powershell
.\.venv\Scripts\python.exe scripts\failure_drills.py --list
.\.venv\Scripts\python.exe scripts\failure_drills.py --only redis_down
.\.venv\Scripts\python.exe scripts\failure_drills.py --only llm_unavailable
.\.venv\Scripts\python.exe scripts\failure_drills.py --only policy_violation
```

To show a live dependency flip instead of the in-process drill, stop a container and watch
the header strip and Grafana Infrastructure dashboard change:

```powershell
docker compose stop redis        # header strip: Redis degraded, ingestion continues
docker compose start redis
docker compose stop redpanda     # header strip: Redpanda down, API keeps serving
docker compose start redpanda
```

## Closing beat

Open the Held-out Evaluation view. It renders `reports/heldout_test/results.json` directly:
3 of 3 test spike events detected, 0 false alerts, 0 false alerts inside benign surges,
median detection delay 30 minutes, and the honest caveat that held-out transaction precision
(0.62) fell well below the validation floor (0.90) because the test spike families were
deliberately harder. No number in the interface is hardcoded.

## Rehearsal checklist

- [ ] `docker compose up -d` healthy, migrations at head
- [ ] `scripts\reset_demo.py --yes` then `scripts\seed_database.py` run cleanly
- [ ] Baseline warm-up replay finished before scene 1
- [ ] Sign-in works for `analyst` and for `admin` (scene 8 needs admin)
- [ ] Scene 2 alert fires within one detector step of the density climb
- [ ] Scene 6 audit trail updates without a page reload
- [ ] Scene 9 drill flips the header strip and recovers
- [ ] Bonus benign-surge beat produces no incident card
- [ ] Full run under 6 minutes, twice in a row, with no manual repair
