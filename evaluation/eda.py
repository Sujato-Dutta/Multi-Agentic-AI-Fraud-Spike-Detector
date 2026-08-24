"""Reproducible development-only EDA and submission figures."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backend.app.config import get_settings
from evaluation.dataio import load_benign_events, load_features, load_split

FIGURE_NAMES = (
    "fraud_rate_timeline.png",
    "hour_seasonality.png",
    "feature_spike_lift.png",
    "class_balance.png",
    "cost_distributions.png",
)


def _window_profile(
    frame: pd.DataFrame, events: pd.DataFrame, event_kind: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    mean_hourly = len(frame) / max(
        (frame["timestamp"].max() - frame["timestamp"].min()).total_seconds() / 3600, 1
    )
    for event in events.itertuples(index=False):
        mask = frame["timestamp"].between(event.start_timestamp, event.end_timestamp)
        selected = frame.loc[mask]
        duration_hours = (event.end_timestamp - event.start_timestamp).total_seconds() / 3600
        rows.append(
            {
                "event_id": event.event_id,
                "event_kind": event_kind,
                "rows": len(selected),
                "duration_hours": float(duration_hours),
                "transactions_per_hour": float(len(selected) / duration_hours),
                "volume_lift": float((len(selected) / duration_hours) / mean_hourly),
                "fraud_rate": float(selected["is_fraud"].mean()),
                "promo_share": float(selected["known_promo_event"].mean()),
            }
        )
    return rows


def compute_eda_summary() -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute facts using development labels only; held-out labels are never loaded."""

    splits = {name: load_split(name) for name in ("train", "validation")}
    joined = []
    spike_events = []
    for name, split in splits.items():
        part = split.joined.assign(split=name)
        joined.append(part)
        spike_events.append(split.spike_events)
    frame = pd.concat(joined, ignore_index=True).sort_values("timestamp", kind="stable")
    spikes = pd.concat(spike_events, ignore_index=True)
    benign = load_benign_events()
    benign_development = benign.loc[benign["split"].isin(["train", "validation"])].copy()

    profiles: list[dict[str, object]] = []
    for name, split in splits.items():
        part = frame.loc[frame["split"].eq(name)]
        profiles.extend(_window_profile(part, split.spike_events, "fraud_spike"))
        profiles.extend(
            _window_profile(
                part, benign_development.loc[benign_development["split"].eq(name)], "benign_surge"
            )
        )
    profile_frame = pd.DataFrame(profiles)

    all_features = pd.concat(
        [load_features(name) for name in ("train", "validation", "test")], ignore_index=True
    )
    span_hours = (
        all_features["timestamp"].max() - all_features["timestamp"].min()
    ).total_seconds() / 3600
    spike_profile = profile_frame.loc[profile_frame["event_kind"].eq("fraud_spike")]
    benign_profile = profile_frame.loc[profile_frame["event_kind"].eq("benign_surge")]

    train_frame = frame.loc[frame["split"].eq("train")]
    train_normal_mask = (
        ~train_frame["is_within_spike_window"].astype(bool)
        & train_frame["benign_event_id"].isna()
    )
    train_benign_profile = benign_profile.loc[benign_profile["event_id"].str.startswith("TRN_")]
    summary = {
        "label_scope": "train_and_validation_only",
        "rows": {name: len(split.features) for name, split in splits.items()},
        "all_feature_rows": len(all_features),
        "mean_transactions_per_hour_all_features": round(len(all_features) / span_hours, 3),
        "fraud_rate": {name: float(split.labels["is_fraud"].mean()) for name, split in splits.items()},
        "spike_event_fraud_rate_range": [
            float(spike_profile["fraud_rate"].min()),
            float(spike_profile["fraud_rate"].max()),
        ],
        "spike_event_volume_lift_range": [
            float(spike_profile["volume_lift"].min()),
            float(spike_profile["volume_lift"].max()),
        ],
        "benign_event_fraud_rate_range": [
            float(benign_profile["fraud_rate"].min()),
            float(benign_profile["fraud_rate"].max()),
        ],
        "benign_event_volume_lift_range": [
            float(benign_profile["volume_lift"].min()),
            float(benign_profile["volume_lift"].max()),
        ],
        "promo_share": {
            "scope": "train_reference",
            "normal": float(train_frame.loc[train_normal_mask, "known_promo_event"].mean()),
            "fraud_spike": float(
                train_frame.loc[
                    train_frame["is_within_spike_window"].astype(bool), "known_promo_event"
                ].mean()
            ),
            "benign_surge": float(
                (train_benign_profile["promo_share"] * train_benign_profile["rows"]).sum()
                / train_benign_profile["rows"].sum()
            ),
            "development_benign_surge_combined": float(
                (benign_profile["promo_share"] * benign_profile["rows"]).sum()
                / benign_profile["rows"].sum()
            ),
        },
        "holdout_note": (
            "Held-out features contribute only to the label-free traffic-rate calculation. "
            "Held-out labels/events remain sealed until Phase 8."
        ),
    }
    return summary, frame, spikes, profile_frame


def _save_timeline(frame: pd.DataFrame, spikes: pd.DataFrame, benign: pd.DataFrame, path: Path) -> None:
    timeline = frame.set_index("timestamp").resample("6h").agg(
        transactions=("transaction_id", "size"), fraud_rate=("is_fraud", "mean")
    )
    fig, ax1 = plt.subplots(figsize=(13, 5))
    ax2 = ax1.twinx()
    ax1.plot(timeline.index, timeline["transactions"], color="#4b8bf5", label="Transactions / 6h")
    ax2.plot(timeline.index, timeline["fraud_rate"] * 100, color="#ef4444", label="Fraud rate")
    for event in spikes.itertuples(index=False):
        ax1.axvspan(event.start_timestamp, event.end_timestamp, color="#ef4444", alpha=0.15)
    for event in benign.itertuples(index=False):
        ax1.axvspan(event.start_timestamp, event.end_timestamp, color="#22c55e", alpha=0.13)
    ax1.set(title="Traffic volume is not enough: spikes and benign surges overlap", ylabel="Transactions")
    ax2.set_ylabel("Fraud rate (%)")
    ax1.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_hour_seasonality(frame: pd.DataFrame, path: Path) -> None:
    normal = frame.loc[
        ~frame["is_within_spike_window"].astype(bool) & frame["benign_event_id"].isna()
    ]
    by_hour = normal.groupby("hour", observed=True).agg(
        fraud_rate=("is_fraud", "mean"), transactions=("transaction_id", "size")
    )
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(by_hour.index, by_hour["fraud_rate"] * 100, color="#8b5cf6")
    ax.set(title="Normal-traffic fraud rate varies by hour", xlabel="Hour (UTC)", ylabel="Fraud rate (%)")
    ax.set_xticks(range(24))
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_feature_lift(frame: pd.DataFrame, path: Path) -> None:
    numeric = [
        "is_new_device", "is_proxy_ip", "billing_shipping_mismatch", "amount_inr",
        "txn_velocity_10m", "txn_velocity_1h", "geo_distance_km", "ip_risk_score",
        "account_changes_24h", "failed_attempts_24h", "amount_zscore_customer",
    ]
    spike = frame.loc[frame["is_spike_injected"].astype(bool), numeric]
    baseline = frame.loc[
        ~frame["is_within_spike_window"].astype(bool) & frame["benign_event_id"].isna(), numeric
    ]
    scale = baseline.std().replace(0, np.nan)
    effect = ((spike.mean() - baseline.mean()) / scale).replace([np.inf, -np.inf], np.nan).dropna()
    effect = effect.reindex(effect.abs().sort_values(ascending=False).index).head(10).sort_values()
    fig, ax = plt.subplots(figsize=(9, 5))
    colors = ["#ef4444" if value > 0 else "#4b8bf5" for value in effect]
    ax.barh(effect.index, effect, color=colors)
    ax.set(title="Injected-spike feature shift vs normal traffic", xlabel="Standardized mean difference")
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_class_balance(frame: pd.DataFrame, path: Path) -> None:
    values = frame.groupby("split", observed=True)["is_fraud"].mean().mul(100)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars = ax.bar(values.index, values.values, color=["#4b8bf5", "#8b5cf6"])
    ax.bar_label(bars, fmt="%.2f%%")
    ax.set(title="Development split class balance", ylabel="Fraud prevalence (%)", ylim=(0, 5))
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_costs(frame: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    fp = frame.loc[frame["is_fraud"].eq(0), "false_positive_cost_if_blocked_inr"]
    loss = frame.loc[frame["is_fraud"].eq(1), "fraud_loss_if_missed_inr"]
    axes[0].hist(fp, bins=40, color="#f59e0b", alpha=0.85)
    axes[0].set(title="False-positive cost proxy", xlabel="INR", ylabel="Transactions")
    axes[1].hist(loss, bins=40, color="#ef4444", alpha=0.85)
    axes[1].set(title="Fraud loss-if-missed proxy", xlabel="INR", ylabel="Fraud transactions")
    for axis in axes:
        axis.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def generate_eda(output_dir: Path | str | None = None) -> dict[str, object]:
    settings = get_settings()
    output = Path(output_dir) if output_dir else settings.report_dir / "figures"
    metrics_dir = settings.report_dir / "metrics"
    output.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    summary, frame, spikes, profiles = compute_eda_summary()
    benign = load_benign_events().loc[lambda data: data["split"].isin(["train", "validation"])]
    _save_timeline(frame, spikes, benign, output / FIGURE_NAMES[0])
    _save_hour_seasonality(frame, output / FIGURE_NAMES[1])
    _save_feature_lift(frame, output / FIGURE_NAMES[2])
    _save_class_balance(frame, output / FIGURE_NAMES[3])
    _save_costs(frame, output / FIGURE_NAMES[4])
    summary["event_profiles"] = profiles.to_dict(orient="records")
    (metrics_dir / "eda_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(generate_eda(), indent=2, default=str))
