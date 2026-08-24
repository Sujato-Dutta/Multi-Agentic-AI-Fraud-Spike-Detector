"""Configured Gemini tier routing and fallback order."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.app.config import Settings, get_settings


class ModelTier(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    ECONOMY = "economy"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    tier: ModelTier
    model_id: str
    input_cost_per_million: float
    output_cost_per_million: float


def route_for(tier: ModelTier, settings: Settings | None = None) -> ModelRoute:
    config = settings or get_settings()
    values = {
        ModelTier.PRIMARY: (
            config.gemini_primary_model,
            config.llm_primary_input_cost_per_million,
            config.llm_primary_output_cost_per_million,
        ),
        ModelTier.SECONDARY: (
            config.gemini_secondary_model,
            config.llm_secondary_input_cost_per_million,
            config.llm_secondary_output_cost_per_million,
        ),
        ModelTier.ECONOMY: (
            config.gemini_economy_model,
            config.llm_economy_input_cost_per_million,
            config.llm_economy_output_cost_per_million,
        ),
    }
    model_id, input_cost, output_cost = values[tier]
    return ModelRoute(tier, model_id, input_cost, output_cost)


def fallback_tiers(start: ModelTier) -> tuple[ModelTier, ...]:
    ordered = (ModelTier.PRIMARY, ModelTier.SECONDARY, ModelTier.ECONOMY)
    return ordered[ordered.index(start) :]
