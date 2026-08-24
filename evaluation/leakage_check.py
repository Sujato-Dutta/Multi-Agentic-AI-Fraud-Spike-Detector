"""Fail-fast checks for temporal and feature leakage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.core.constants import LABEL_COLUMNS, RAW_ID_COLUMNS
from backend.app.ml.fraud.features import build_features
from evaluation.dataio import load_features, load_split


@dataclass(frozen=True)
class LeakageCheckResult:
    train_rows: int
    validation_rows: int
    test_feature_rows: int
    model_feature_count: int
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


def run_leakage_checks(data_dir: Path | str | None = None) -> LeakageCheckResult:
    train = load_split("train", data_dir)
    validation = load_split("validation", data_dir)
    test_features = load_features("test", data_dir)

    for name, features in (("train", train.features), ("validation", validation.features)):
        leaked = LABEL_COLUMNS.intersection(features.columns)
        if leaked:
            raise AssertionError(f"{name} feature file contains labels: {sorted(leaked)}")
        model_frame = build_features(features)
        forbidden = (LABEL_COLUMNS | RAW_ID_COLUMNS).intersection(model_frame.columns)
        if forbidden:
            raise AssertionError(f"{name} model matrix leaks columns: {sorted(forbidden)}")

    train_end = train.features["timestamp"].max()
    validation_start = validation.features["timestamp"].min()
    validation_end = validation.features["timestamp"].max()
    test_start = test_features["timestamp"].min()
    if not train_end < validation_start < validation_end < test_start:
        raise AssertionError("Splits are not strictly chronological and non-overlapping")

    return LeakageCheckResult(
        train_rows=len(train.features),
        validation_rows=len(validation.features),
        test_feature_rows=len(test_features),
        model_feature_count=build_features(train.features.head(2)).shape[1],
        train_end=train_end.isoformat(),
        validation_start=validation_start.isoformat(),
        validation_end=validation_end.isoformat(),
        test_start=test_start.isoformat(),
    )
