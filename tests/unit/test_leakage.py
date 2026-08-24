from __future__ import annotations

import pandas as pd
import pytest

from backend.app.core.constants import LABEL_COLUMNS, RAW_ID_COLUMNS
from backend.app.ml.fraud.features import FeatureContractError, build_features
from evaluation.dataio import (
    load_benign_events,
    load_split,
    load_test_benign_events,
    load_test_holdout,
)
from evaluation.leakage_check import run_leakage_checks


def test_feature_contract_derives_safe_network_group_and_drops_raw_identifiers() -> None:
    features = load_split("train").features.head(10)
    model_frame = build_features(features)

    assert not (set(model_frame) & RAW_ID_COLUMNS)
    assert not (set(model_frame) & LABEL_COLUMNS)
    assert "timestamp" not in model_frame
    assert "ip_cluster_group" in model_frame
    assert str(model_frame["ip_cluster_group"].dtype) == "category"
    assert str(model_frame["payment_method"].dtype) == "category"
    assert model_frame["ip_cluster_group"].astype(str).str.match(r"IP\d{2}").all()


def test_feature_contract_rejects_evaluation_labels_and_unknown_strings() -> None:
    with pytest.raises(FeatureContractError, match="Evaluation-only"):
        build_features(pd.DataFrame({"amount_inr": [10.0], "is_fraud": [1]}))
    with pytest.raises(FeatureContractError, match="explicit contract"):
        build_features(pd.DataFrame({"amount_inr": [10.0], "unreviewed_text": ["unsafe"]}))


def test_split_ordering_and_holdout_guard() -> None:
    result = run_leakage_checks()

    assert result.train_rows == 18_000
    assert result.validation_rows == result.test_feature_rows == 6_000
    assert result.model_feature_count == 26
    assert result.train_end < result.validation_start < result.validation_end < result.test_start
    assert set(load_benign_events()["split"]) == {"train", "validation"}
    with pytest.raises(ValueError, match="Phase 8"):
        load_benign_events("test")  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="sealed"):
        load_test_benign_events("not-the-phase-8-token")
    with pytest.raises(PermissionError, match="sealed"):
        load_test_holdout("not-the-phase-8-token")
