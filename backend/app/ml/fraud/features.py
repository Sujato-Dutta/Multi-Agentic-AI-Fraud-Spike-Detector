"""Leakage-safe feature contract shared by training and serving."""

from __future__ import annotations

import pandas as pd

from backend.app.core.constants import (
    CATEGORICAL_COLUMNS,
    LABEL_COLUMNS,
    NON_MODEL_COLUMNS,
    RAW_ID_COLUMNS,
)


class FeatureContractError(ValueError):
    """Raised when model inputs violate the feature contract."""


def build_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return an XGBoost-ready frame without labels, timestamps, or high-cardinality IDs.

    `ip_cluster_group` is the only permitted derivative of a raw identifier. Categorical dtypes are
    retained for XGBoost native categorical splits. The function never mutates its input.
    """

    leaked = sorted(LABEL_COLUMNS.intersection(frame.columns))
    if leaked:
        raise FeatureContractError(f"Evaluation-only columns in model input: {leaked}")

    result = frame.copy()
    if "ip_cluster_id" in result:
        result["ip_cluster_group"] = (
            result["ip_cluster_id"].astype("string").str.extract(r"^(IP\d{2})", expand=False)
        )

    result = result.drop(columns=list(NON_MODEL_COLUMNS.intersection(result.columns)), errors="ignore")
    unexpected_objects = set(result.select_dtypes(include=["object", "string"]).columns) - set(
        CATEGORICAL_COLUMNS
    )
    if unexpected_objects:
        raise FeatureContractError(
            f"Unexpected string features require an explicit contract: {sorted(unexpected_objects)}"
        )

    for column in CATEGORICAL_COLUMNS:
        if column in result:
            result[column] = result[column].astype("category")
    for column in result.columns.difference(CATEGORICAL_COLUMNS):
        result[column] = pd.to_numeric(result[column], errors="raise")

    forbidden_output = RAW_ID_COLUMNS.intersection(result.columns) | LABEL_COLUMNS.intersection(
        result.columns
    )
    if forbidden_output:
        raise FeatureContractError(f"Forbidden columns survived feature construction: {forbidden_output}")
    if result.empty or result.isna().all(axis=None):
        raise FeatureContractError("Feature matrix is empty")
    return result


def fit_category_schema(frame: pd.DataFrame) -> dict[str, list[str]]:
    """Capture training categories so calibration, validation, and serving use identical dtypes."""

    return {
        column: sorted(frame[column].dropna().astype("string").unique().tolist())
        for column in CATEGORICAL_COLUMNS
        if column in frame
    }


def apply_category_schema(
    frame: pd.DataFrame, schema: dict[str, list[str]]
) -> pd.DataFrame:
    """Apply known categories; unseen values become missing and follow XGBoost's missing branch."""

    result = frame.copy()
    for column, categories in schema.items():
        if column in result:
            result[column] = pd.Categorical(result[column].astype("string"), categories=categories)
    return result
