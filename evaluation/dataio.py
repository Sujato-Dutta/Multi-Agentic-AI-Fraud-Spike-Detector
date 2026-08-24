"""Single guarded boundary for loading benchmark data.

Phases 1-7 may load train/validation labels and all feature files. Raw test labels and test event
metadata require the explicit Phase 8 acknowledgement token.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from backend.app.config import get_settings

DevelopmentSplit = Literal["train", "validation"]
FeatureSplit = Literal["train", "validation", "test"]
HOLDOUT_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_THIS_IS_THE_SINGLE_PHASE_8_HOLDOUT_EVALUATION"


@dataclass(frozen=True)
class DatasetSplit:
    features: pd.DataFrame
    labels: pd.DataFrame
    spike_events: pd.DataFrame

    @property
    def joined(self) -> pd.DataFrame:
        joined = self.features.merge(
            self.labels, on="transaction_id", how="inner", validate="one_to_one"
        )
        if len(joined) != len(self.features):
            raise ValueError("Feature/label join lost rows; transaction IDs are inconsistent")
        return joined.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _data_dir(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else get_settings().data_dir


def _read_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["timestamp"])
    if frame["transaction_id"].duplicated().any():
        raise ValueError(f"Duplicate transaction IDs in {path}")
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError(f"Transactions are not chronological in {path}")
    return frame


def _read_labels(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if frame["transaction_id"].duplicated().any():
        raise ValueError(f"Duplicate label transaction IDs in {path}")
    return frame


def _read_events(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["start_timestamp", "end_timestamp"])
    if (frame["end_timestamp"] <= frame["start_timestamp"]).any():
        raise ValueError(f"Invalid event range in {path}")
    return frame.sort_values("start_timestamp", kind="stable").reset_index(drop=True)


def load_features(split: FeatureSplit, data_dir: Path | str | None = None) -> pd.DataFrame:
    """Load feature-only data; held-out features are safe for split-order checks."""

    base = _data_dir(data_dir)
    folder = "test_holdout" if split == "test" else split
    filename = "test_features.csv" if split == "test" else f"{split}_features.csv"
    return _read_features(base / folder / filename)


def load_split(split: DevelopmentSplit, data_dir: Path | str | None = None) -> DatasetSplit:
    """Load a development split. The type and runtime guard exclude the test holdout."""

    if split not in {"train", "validation"}:
        raise ValueError("Only train/validation are available without the Phase 8 holdout guard")
    base = _data_dir(data_dir)
    return DatasetSplit(
        features=load_features(split, base),
        labels=_read_labels(base / split / f"{split}_labels.csv"),
        spike_events=_read_events(base / split / f"{split}_spike_events.csv"),
    )


def load_benign_events(
    split: DevelopmentSplit | None = None, data_dir: Path | str | None = None
) -> pd.DataFrame:
    """Load development-only benign windows without reading held-out annotations."""

    events = _read_events(
        _data_dir(data_dir) / "metadata" / "development_benign_surge_events.csv"
    )
    if split is not None:
        if split not in {"train", "validation"}:
            raise ValueError("Test benign windows require the Phase 8 holdout guard")
        events = events.loc[events["split"].eq(split)].reset_index(drop=True)
    return events


def load_test_benign_events(
    acknowledgement: str, data_dir: Path | str | None = None
) -> pd.DataFrame:
    """Load test benign-surge annotations only under the Phase 8 acknowledgement."""

    if acknowledgement != HOLDOUT_ACKNOWLEDGEMENT:
        raise PermissionError(
            "Held-out benign windows are sealed. Pass the exact Phase 8 acknowledgement token."
        )
    events = _read_events(_data_dir(data_dir) / "metadata" / "benign_surge_events.csv")
    return events.loc[events["split"].eq("test")].reset_index(drop=True)


def load_test_holdout(
    acknowledgement: str, data_dir: Path | str | None = None
) -> DatasetSplit:
    """Load held-out test labels only for the single, frozen Phase 8 evaluation."""

    if acknowledgement != HOLDOUT_ACKNOWLEDGEMENT:
        raise PermissionError(
            "Held-out labels are sealed. Pass the exact Phase 8 acknowledgement token."
        )
    base = _data_dir(data_dir) / "test_holdout"
    return DatasetSplit(
        features=_read_features(base / "test_features.csv"),
        labels=_read_labels(base / "DO_NOT_USE_FOR_TUNING_test_labels.csv"),
        spike_events=_read_events(base / "DO_NOT_USE_FOR_TUNING_test_spike_events.csv"),
    )
