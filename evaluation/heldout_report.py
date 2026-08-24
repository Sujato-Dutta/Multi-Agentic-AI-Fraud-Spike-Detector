"""The single held-out evaluation.

This module is the only caller of the sealed test-label loaders. It runs once, at freeze
time, and writes an immutable record: ``results.json`` with the commit hash and UTC
timestamp, a human-readable ``report.md``, figures, and an append-only ``ACCESS_LOG.md``
entry. A second entry in that log means the honesty protocol was broken and must be
disclosed.

Every number here is measured. Where a metric cannot be produced without live
infrastructure or credentials, the report records that explicitly instead of estimating.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.ml.fraud.predictor import FraudPredictor
from backend.app.ml.policy.contextual_bandit import LinUCBPolicy
from backend.app.safety.policy_engine import PolicyContext, PolicyEngine
from backend.app.services.evaluation_service import (
    ActionEffects,
    RewardCalculator,
)
from evaluation.dataio import (
    HOLDOUT_ACKNOWLEDGEMENT,
    DatasetSplit,
    load_features,
    load_test_benign_events,
    load_test_holdout,
)
from evaluation.evaluate_business_cost import evaluate_business_cost
from evaluation.evaluate_fraud import aligned_labels, evaluate_fraud_model
from evaluation.evaluate_spikes import evaluate_spike_replay
from evaluation.leakage_check import run_leakage_checks
from evaluation.metrics import CostMetrics, net_risk_benefit
from evaluation.replay import replay_detector

REPORT_DIRNAME = "heldout_test"
SELECTED_OPERATING_POINT = "precision_floor"
CONSERVATIVE_PRODUCTION_ACTION = "human_escalation"


def _git_state() -> tuple[str, str]:
    """Return (commit, working_tree_state); 'unavailable' when this is not a git checkout."""

    def run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = run(["git", "rev-parse", "HEAD"])
    if commit is None:
        return "unavailable", "unavailable"
    status = run(["git", "status", "--porcelain"])
    if status is None:
        return commit, "unavailable"
    return commit, "clean" if not status else "dirty"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_previous_reads(access_log: Path) -> int:
    """Count prior sealed reads from the log's per-read headings.

    The tripwire must fail loudly rather than quietly: if the log exists but no heading
    parses, treat that as an unknown history instead of reporting a first read.
    """

    if not access_log.exists():
        return 0
    lines = access_log.read_text(encoding="utf-8").splitlines()
    headings = sum(1 for line in lines if line.startswith("## Read "))
    if headings:
        return headings
    legacy_rows = sum(1 for line in lines if line.startswith("| 2"))
    if legacy_rows:
        return legacy_rows
    raise SystemExit(
        f"{access_log} exists but no read entries could be parsed. Refusing to run, because "
        "an unparsed log would understate how many times the sealed labels were read."
    )


def _holdout_incidents(
    replay: Any,
    holdout: DatasetSplit,
    calibrated: np.ndarray,
    calculator: RewardCalculator,
) -> list[dict[str, Any]]:
    """Build one reward-bearing record per held-out alert window from sealed labels.

    The context vector uses the same feature definitions as ``training/train_reward_model``
    so the learned candidate policy is scored on in-distribution inputs rather than a
    synthetic placeholder.
    """

    joined = holdout.joined.merge(
        pd.DataFrame(
            {
                "transaction_id": holdout.features["transaction_id"],
                "risk_probability": calibrated,
            }
        ),
        on="transaction_id",
        how="left",
        validate="one_to_one",
    )
    split_density = float(np.mean(calibrated))
    hours = max(
        (holdout.features["timestamp"].max() - holdout.features["timestamp"].min()).total_seconds()
        / 3600,
        1,
    )
    hourly_volume = len(holdout.features) / hours

    incidents: list[dict[str, Any]] = []
    prior_best_rewards: list[float] = []
    for alert in replay.alerts:
        start = pd.Timestamp(alert.window_start)
        end = pd.Timestamp(alert.fire_timestamp)
        rows = joined.loc[joined["timestamp"].between(start, end, inclusive="both")]
        if rows.empty:
            continue
        records = rows.to_dict("records")
        rewards = {
            item.action: item.model_dump(mode="json")
            for item in calculator.counterfactuals(records)
        }
        probabilities = rows["risk_probability"].to_numpy(dtype=float)
        duration_hours = max((end - start).total_seconds() / 3600, 1)
        context = {
            "fraud_probability_mean": float(np.mean(probabilities)),
            "fraud_probability_max": float(np.max(probabilities)),
            "density_lift": float(np.mean(probabilities) / max(split_density, 1e-9)),
            "volume_lift": float(len(rows) / max(hourly_volume * duration_hours, 1)),
            "segment_support": float(len(rows)),
            "segment_breadth": float(min(1.0, len(rows) / max(len(holdout.features), 1))),
            "amount_mean_inr": float(rows["amount_inr"].mean()),
            "amount_max_inr": float(rows["amount_inr"].max()),
            "agent_confidence": 0.0,
            "grounding_score": 0.0,
            "historical_segment_fraud_rate": float(rows["is_fraud"].mean()),
            "promotion_context": float(rows["known_promo_event"].mean()),
            "similar_incident_mean_reward": (
                float(np.mean(prior_best_rewards)) if prior_best_rewards else 0.0
            ),
            "similar_incident_rejection_rate": 0.0,
        }
        prior_best_rewards.append(max(item["total_reward_inr"] for item in rewards.values()))
        incidents.append(
            {
                "alert_id": alert.alert_id,
                "context": context,
                "rewards": rewards,
                "fraud_loss_total_inr": float(
                    rows.loc[rows["is_fraud"].eq(1), "fraud_loss_if_missed_inr"].sum()
                ),
                "legitimate_value_inr": float(rows.loc[rows["is_fraud"].eq(0), "amount_inr"].sum()),
            }
        )
    return incidents


def _policy_section(
    incidents: list[dict[str, Any]], settings: Settings, assumptions_version: str
) -> dict[str, Any]:
    """Compare the operative production ranking with the shadow candidate, honestly."""

    safety = PolicyEngine.default()
    candidate: LinUCBPolicy | None = None
    candidate_error: str | None = None
    if settings.candidate_policy_path.exists():
        try:
            candidate = LinUCBPolicy.load(
                settings.candidate_policy_path, assumptions_version=assumptions_version
            )
        except Exception as exc:  # noqa: BLE001 - report the failure rather than guessing
            candidate_error = f"{type(exc).__name__}: {exc}"

    production_uses_artifact = settings.production_policy_path.exists()
    production_policy: LinUCBPolicy | None = None
    if production_uses_artifact:
        try:
            production_policy = LinUCBPolicy.load(
                settings.production_policy_path, assumptions_version=assumptions_version
            )
        except Exception:  # noqa: BLE001 - fall back to the conservative fixed ranking
            production_policy = None
            production_uses_artifact = False

    production_rewards: list[float] = []
    candidate_rewards: list[float] = []
    violations = 0
    production_actions: list[str] = []
    candidate_actions: list[str] = []

    for incident in incidents:
        context = incident["context"]
        production_action = (
            production_policy.greedy_action(context)
            if production_policy is not None
            else CONSERVATIVE_PRODUCTION_ACTION
        )
        production_actions.append(production_action)
        production_rewards.append(float(incident["rewards"][production_action]["total_reward_inr"]))

        if candidate is not None:
            candidate_action = candidate.greedy_action(context)
            candidate_actions.append(candidate_action)
            candidate_rewards.append(
                float(incident["rewards"][candidate_action]["total_reward_inr"])
            )

        decision = safety.evaluate(
            production_action,
            PolicyContext(
                affected_legitimate_value_inr=incident["legitimate_value_inr"],
                fraud_exposure_inr=incident["fraud_loss_total_inr"],
                segment_breadth=incident["context"]["segment_breadth"],
                grounding_score=1,
                confidence_score=1,
                novelty_score=1,
                actor_role="lead_analyst",
            ),
        )
        violations += int(decision.decision == "deny")

    notes = [
        (
            "The operative action is the production ranking; the candidate is scored for "
            "comparison only and never executes."
        ),
        (
            "No automatic promotion path exists in code; promotion and rollback require an "
            "authenticated admin."
        ),
    ]
    if candidate_error:
        notes.append(f"Candidate policy artifact could not be loaded: {candidate_error}")
    if not production_uses_artifact:
        notes.append(
            "No production policy artifact is registered, so production is the conservative fixed "
            "ranking headed by human_escalation."
        )

    return {
        "evaluated_incidents": len(incidents),
        "production_action": production_actions[0] if production_actions else None,
        "candidate_action": candidate_actions[0] if candidate_actions else None,
        "production_expected_reward_inr": round(float(np.mean(production_rewards)), 2)
        if production_rewards
        else None,
        "candidate_expected_reward_inr": round(float(np.mean(candidate_rewards)), 2)
        if candidate_rewards
        else None,
        "production_uses_learned_artifact": production_uses_artifact,
        "safety_policy_violations": violations,
        "automatic_promotion": False,
        "assumptions_version": assumptions_version,
        "notes": notes,
    }


def _sensitivity(
    costs: CostMetrics, reviewed_incidents: int, stepped_up: int, settings: Settings
) -> list[dict[str, Any]]:
    """Recompute net risk benefit while varying one stated assumption at a time."""

    rows: list[dict[str, Any]] = []
    for value in (0.0, 125.0, 250.0, 500.0, 1000.0):
        rows.append(
            {
                "parameter": "analyst_review_cost_inr",
                "value": value,
                "net_risk_benefit_inr": round(
                    net_risk_benefit(
                        costs.fraud_exposure_captured_inr,
                        costs.false_positive_cost_inr,
                        reviewed_incidents * value,
                        stepped_up * settings.customer_friction_cost_inr,
                    ),
                    2,
                ),
            }
        )
    for value in (0.0, 20.0, 40.0, 80.0, 160.0):
        rows.append(
            {
                "parameter": "customer_friction_cost_inr",
                "value": value,
                "net_risk_benefit_inr": round(
                    net_risk_benefit(
                        costs.fraud_exposure_captured_inr,
                        costs.false_positive_cost_inr,
                        reviewed_incidents * settings.analyst_review_cost_inr,
                        stepped_up * value,
                    ),
                    2,
                ),
            }
        )
    return rows


def _figures(
    output_dir: Path,
    labels: pd.DataFrame,
    calibrated: np.ndarray,
    replay: Any,
    holdout: DatasetSplit,
    benign_events: pd.DataFrame,
    sensitivity: list[dict[str, Any]],
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    plt.style.use("dark_background")

    # 1. Precision-recall curve at the sealed operating point.
    from sklearn.metrics import precision_recall_curve

    precision, recall, _ = precision_recall_curve(labels["is_fraud"], calibrated)
    figure, axis = plt.subplots(figsize=(6, 4), dpi=140)
    axis.plot(recall, precision, color="#37d6c4", linewidth=2)
    axis.set_xlabel("Recall")
    axis.set_ylabel("Precision")
    axis.set_title("Held-out precision-recall (calibrated)")
    axis.grid(alpha=0.18)
    path = output_dir / "precision_recall.png"
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
    written.append(path.name)

    # 2. Risk density over the holdout with spike and benign windows shaded.
    frame = holdout.features[["timestamp"]].copy()
    frame["risk"] = calibrated
    hourly = frame.set_index("timestamp")["risk"].resample("1h").mean().dropna()
    figure, axis = plt.subplots(figsize=(10, 4), dpi=140)
    axis.plot(hourly.index, hourly.to_numpy(), color="#37d6c4", linewidth=1.4, label="mean risk density")
    for _, event in holdout.spike_events.iterrows():
        axis.axvspan(event["start_timestamp"], event["end_timestamp"], color="#ff5470", alpha=0.16)
    for _, event in benign_events.iterrows():
        axis.axvspan(event["start_timestamp"], event["end_timestamp"], color="#ffb340", alpha=0.14)
    for alert in replay.alerts:
        axis.axvline(pd.Timestamp(alert.fire_timestamp), color="#8b7dfb", linewidth=1.1, linestyle="--")
    axis.set_title("Held-out risk density · red = fraud spike, amber = benign surge, dashed = alert")
    axis.set_ylabel("Mean calibrated risk")
    axis.grid(alpha=0.15)
    figure.autofmt_xdate()
    path = output_dir / "risk_density_timeline.png"
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
    written.append(path.name)

    # 3. Net risk benefit sensitivity to the stated cost assumptions.
    review_rows = [row for row in sensitivity if row["parameter"] == "analyst_review_cost_inr"]
    if review_rows:
        figure, axis = plt.subplots(figsize=(6, 4), dpi=140)
        axis.bar(
            [str(row["value"]) for row in review_rows],
            [row["net_risk_benefit_inr"] for row in review_rows],
            color="#8b7dfb",
        )
        axis.set_xlabel("Analyst review cost assumption (INR per incident)")
        axis.set_ylabel("Net risk benefit (INR)")
        axis.set_title("Sensitivity to the analyst review cost assumption")
        axis.grid(alpha=0.15, axis="y")
        path = output_dir / "sensitivity_review_cost.png"
        figure.tight_layout()
        figure.savefig(path)
        plt.close(figure)
        written.append(path.name)

    return written


def _markdown(report: dict[str, Any]) -> str:
    point = report["transaction"][report["transaction"]["selected_operating_point"]]
    metrics = point["metrics"]
    events = report["events"]["metrics"]
    business = report["business"]
    policy = report["policy"]
    lines = [
        "# Held-out Test Evaluation",
        "",
        (
            f"Generated `{report['generated_at']}` · commit `{report['commit']}` · "
            f"working tree `{report['working_tree']}`."
        ),
        "",
        (
            "Sealed evaluation of the test holdout. Model, thresholds, detector parameters, and "
            "policies were frozen before it ran."
        ),
        "",
        (
            f"Reads of the sealed labels so far: **{report['integrity']['access_log_entries']}** "
            f"(this read: {report['read_reason']}). Every read and its reason is listed in "
            "`ACCESS_LOG.md`."
        ),
        "",
        "## Transaction level",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Operating point | {point['operating_point']} |",
        f"| Threshold ({point['threshold_score_space']}) | {point['threshold']:.4f} |",
        f"| Precision | {metrics['precision']:.4f} |",
        f"| Recall | {metrics['recall']:.4f} |",
        f"| F1 | {metrics['f1']:.4f} |",
        f"| PR-AUC (calibrated) | {metrics['pr_auc']:.4f} |",
        f"| ROC-AUC (calibrated) | {metrics['roc_auc']:.4f} |",
        f"| False positives | {metrics['false_positives']} |",
        f"| False negatives | {metrics['false_negatives']} |",
        f"| FP rate | {metrics['false_positive_rate']:.4f} |",
        f"| FN rate | {metrics['false_negative_rate']:.4f} |",
        "",
        "## Spike / event level",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Event precision | {events['precision']:.4f} |",
        f"| Event recall | {events['recall']:.4f} |",
        f"| Matched events | {events['matched_events']} / {events['total_events']} |",
        f"| False alerts | {events['false_alerts']} |",
        f"| **False alerts inside benign surges** | **{events['benign_window_false_alerts']}** |",
        f"| Continuation alerts (excluded) | {events['continuation_alerts']} |",
        f"| Median detection delay | {_fmt_minutes(events['median_delay_minutes'])} |",
        f"| P90 detection delay | {_fmt_minutes(events['p90_delay_minutes'])} |",
        "",
        "## Business outcome",
        "",
        "| Metric | INR |",
        "|---|---:|",
        f"| Fraud value captured | {business['fraud_exposure_captured_inr']:,.2f} |",
        f"| Fraud value missed | {business['fraud_loss_missed_inr']:,.2f} |",
        f"| False-positive cost | {business['false_positive_cost_inr']:,.2f} |",
        f"| Legitimate value disrupted | {business['legitimate_value_disrupted_inr']:,.2f} |",
        f"| Analyst review cost (assumption) | {business['analyst_review_cost_inr']:,.2f} |",
        f"| Customer friction cost (assumption) | {business['customer_friction_cost_inr']:,.2f} |",
        f"| **Net risk benefit** | **{business['net_risk_benefit_inr']:,.2f}** |",
        "",
        (
            "Operational costs are the documented assumptions in "
            "`reports/COST_ASSUMPTIONS.md`, not measured values. Sensitivity is reported below."
        ),
        "",
        "## Policy and safety",
        "",
        f"- Evaluated held-out incidents: {policy['evaluated_incidents']}",
        (
            f"- Production action: `{policy['production_action']}` "
            f"(learned artifact: {policy['production_uses_learned_artifact']})"
        ),
        f"- Candidate action (shadow only): `{policy['candidate_action']}`",
        f"- Production expected reward: {_fmt_money(policy['production_expected_reward_inr'])}",
        f"- Candidate expected reward: {_fmt_money(policy['candidate_expected_reward_inr'])}",
        f"- Deterministic safety-policy violations: **{policy['safety_policy_violations']}**",
        f"- Automatic promotion path: {policy['automatic_promotion']}",
        "",
        *[f"- {note}" for note in policy["notes"]],
        "",
        "## Assumption sensitivity",
        "",
        "| Assumption | Value | Net risk benefit (INR) |",
        "|---|---:|---:|",
        *[
            f"| {row['parameter']} | {row['value']} | {row['net_risk_benefit_inr']:,.2f} |"
            for row in report["sensitivity"]
        ],
        "",
        "## Agent metrics",
        "",
        *[f"- {key}: {value}" for key, value in report["agent"].items()],
        "",
        "## Integrity",
        "",
        *[f"- {note}" for note in report["integrity"]["notes"]],
        "",
        f"Figures: {', '.join(report['figures'])}",
        "",
        (
            "Caveats identified after this run are recorded in `CAVEATS.md` beside this file, "
            "including why the policy comparison is not a serving-time forecast."
        ),
        "",
    ]
    return "\n".join(lines)


def _fmt_minutes(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} min"


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"₹{value:,.2f}"


def run_heldout_evaluation(
    settings: Settings | None = None,
    *,
    allow_dirty: bool = False,
    reason: str = "initial sealed evaluation",
) -> dict[str, Any]:
    config = settings or get_settings()
    commit, tree_state = _git_state()
    if tree_state == "dirty" and not allow_dirty:
        raise SystemExit(
            "Working tree is dirty. Commit or stash before the sealed evaluation, or pass "
            "--allow-dirty and disclose it in the report."
        )

    # Count prior reads before touching sealed data, so an unparsable ledger aborts the run
    # while the labels are still unread and no derived artifact has been written.
    output_dir = config.report_dir / REPORT_DIRNAME
    access_log = output_dir / "ACCESS_LOG.md"
    previous_entries = _count_previous_reads(access_log)

    leakage = run_leakage_checks()
    model_path = config.model_dir / "fraud" / "fraud_model.joblib"
    predictor = FraudPredictor.load(model_path)
    effects = ActionEffects.from_yaml(config.action_effects_path)
    calculator = RewardCalculator(effects, config)

    # The single sealed read of held-out labels and benign-window annotations.
    holdout = load_test_holdout(HOLDOUT_ACKNOWLEDGEMENT, config.data_dir)
    benign_events = load_test_benign_events(HOLDOUT_ACKNOWLEDGEMENT, config.data_dir)

    transaction = {
        point: evaluate_fraud_model(holdout, predictor, point)
        for point in ("precision_floor", "cost_optimal")
    }
    transaction["selected_operating_point"] = SELECTED_OPERATING_POINT

    labels = aligned_labels(holdout)
    calibrated = predictor.predict_proba(holdout.features)

    # Detector replay: reference is all development data, chronologically before the holdout.
    reference = pd.concat(
        [load_features("train", config.data_dir), load_features("validation", config.data_dir)],
        ignore_index=True,
    ).sort_values("timestamp", kind="stable")
    replay = replay_detector(holdout.features, reference, predictor, config)
    events = evaluate_spike_replay(
        replay,
        holdout.spike_events,
        benign_events,
        stream_start=holdout.features["timestamp"].min(),
        stream_end=holdout.features["timestamp"].max(),
        grace_minutes=config.event_match_grace_minutes,
    )

    selected = transaction[SELECTED_OPERATING_POINT]
    costs = CostMetrics(**selected["costs"])
    reviewed_incidents = len(replay.alerts)
    stepped_up = int(selected["metrics"]["false_positives"])
    business = evaluate_business_cost(
        costs,
        reviewed_incidents=reviewed_incidents,
        stepped_up_legitimate_customers=stepped_up,
        settings=config,
    )
    sensitivity = _sensitivity(costs, reviewed_incidents, stepped_up, config)

    incidents = _holdout_incidents(replay, holdout, calibrated, calculator)
    policy = _policy_section(incidents, config, effects.version)

    agent = {
        "deterministic_policy_violations": policy["safety_policy_violations"],
        "llm_authorized_actions": 0,
        "financial_values_produced_by_llm": 0,
        "live_agent_narrative_metrics": (
            "not measured in this run: grounding accuracy, verification rejection rate, "
            "recommendation acceptance and override rates require the running stack with an LLM "
            "credential, which was unavailable at freeze time"
        ),
    }

    figures = _figures(
        output_dir / "figures", labels, calibrated, replay, holdout, benign_events, sensitivity
    )

    integrity_notes = [
        (
            "Held-out labels and benign-window annotations are reachable only through the guarded "
            "loader, and this script is their only caller."
        ),
        "Volume lift is emitted as context only and cannot trigger an alert.",
        (
            "Promotion context raises the required density lift and never suppresses a "
            "qualifying spike."
        ),
        (
            "The policy comparison scores both rankings using offline-training context semantics "
            "(segment fraud rate from window labels, zeroed agent confidence, whole-holdout lift "
            "baselines). Serving computes several of these features differently, so treat it as a "
            "like-for-like ranking comparison, not a serving-time forecast. Nothing is promoted "
            "from it."
        ),
        (
            f"Chronology verified: train ends {leakage.train_end}, validation "
            f"{leakage.validation_start} to {leakage.validation_end}, test starts "
            f"{leakage.test_start}."
        ),
    ]
    if tree_state == "unavailable":
        integrity_notes.append(
            "Git metadata was unavailable in this workspace, so the commit hash and working-tree "
            "state could not be recorded. This is disclosed rather than substituted."
        )
    if tree_state == "dirty":
        integrity_notes.append(
            "The working tree was dirty and the run was forced with --allow-dirty; treat the "
            "commit reference as approximate."
        )
    if previous_entries:
        integrity_notes.append(
            f"DISCLOSURE: this is read {previous_entries + 1} of the held-out labels. Reason for "
            f"this read: {reason}. Every read is listed in ACCESS_LOG.md."
        )

    report: dict[str, Any] = {
        "report_type": "phase8_heldout_evaluation",
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": commit,
        "working_tree": tree_state,
        "acknowledgement": HOLDOUT_ACKNOWLEDGEMENT,
        "read_reason": reason,
        "artifacts": {
            "fraud_model": {"path": str(model_path), "sha256": _sha256(model_path)},
            "action_effects_version": effects.version,
            "candidate_policy_present": config.candidate_policy_path.exists(),
            "production_policy_present": config.production_policy_path.exists(),
        },
        "dataset": {
            "rows": len(holdout.features),
            "spike_events": len(holdout.spike_events),
            "benign_events": len(benign_events),
            "start": holdout.features["timestamp"].min().isoformat(),
            "end": holdout.features["timestamp"].max().isoformat(),
        },
        "detector_config": {
            "window_minutes": config.detector_window_minutes,
            "slide_minutes": config.detector_slide_minutes,
            "min_support": config.detector_min_support,
            "density_lift": config.detector_lift_threshold,
            "extreme_lift": config.detector_extreme_lift,
            "alpha": config.detector_alpha,
            "confirmation_windows": config.detector_confirm_windows,
            "promo_lift_margin": config.detector_promo_lift_margin,
        },
        "transaction": transaction,
        "events": events,
        "business": business,
        "sensitivity": sensitivity,
        "policy": policy,
        "agent": agent,
        "figures": figures,
        "integrity": {
            "protocol": (
                "Fit on train, calibrate on the train tail, select thresholds on validation, touch "
                "the test holdout once."
            ),
            "leakage": leakage.to_dict(),
            "access_log_entries": previous_entries + 1,
            "notes": integrity_notes,
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8")

    if not access_log.exists():
        access_log.write_text(
            "# Held-out Access Log\n\n"
            "Append-only. One read is expected. Any additional read means the single-read protocol "
            "was broken and must be disclosed, with its reason recorded below.\n",
            encoding="utf-8",
        )
    with access_log.open("a", encoding="utf-8") as stream:
        stream.write(
            f"\n## Read {previous_entries + 1}\n\n"
            f"- Timestamp (UTC): `{report['generated_at']}`\n"
            f"- Commit: `{commit}`\n"
            f"- Working tree: `{tree_state}`\n"
            f"- Fraud model SHA-256: `{report['artifacts']['fraud_model']['sha256']}`\n"
            f"- Results SHA-256: `{_sha256(results_path)}`\n"
            f"- Reason: {reason}\n"
        )
    return report


def _render_only() -> int:
    """Regenerate the human-readable report from the sealed results without re-reading labels."""

    output_dir = get_settings().report_dir / REPORT_DIRNAME
    results_path = output_dir / "results.json"
    if not results_path.exists():
        raise SystemExit(f"No sealed results at {results_path}; run the evaluation first.")
    report = json.loads(results_path.read_text(encoding="utf-8"))
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "rendered",
                "source": str(results_path),
                "sealed_label_reads": report["integrity"]["access_log_entries"],
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run despite an unclean working tree; the report records that it was forced",
    )
    parser.add_argument(
        "--reason",
        default="initial sealed evaluation",
        help="Recorded in results.json and ACCESS_LOG.md to justify this read of sealed labels",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help=(
            "Rewrite report.md from the existing results.json. Reads no sealed labels and adds no "
            "access-log entry."
        ),
    )
    args = parser.parse_args()
    if args.render_only:
        return _render_only()
    report = run_heldout_evaluation(allow_dirty=args.allow_dirty, reason=args.reason)
    selected = report["transaction"][report["transaction"]["selected_operating_point"]]["metrics"]
    events = report["events"]["metrics"]
    print(
        json.dumps(
            {
                "status": "ok",
                "precision": round(selected["precision"], 4),
                "recall": round(selected["recall"], 4),
                "pr_auc": round(selected["pr_auc"], 4),
                "event_recall": round(events["recall"], 4),
                "false_alerts": events["false_alerts"],
                "benign_window_false_alerts": events["benign_window_false_alerts"],
                "net_risk_benefit_inr": report["business"]["net_risk_benefit_inr"],
                "safety_policy_violations": report["policy"]["safety_policy_violations"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
