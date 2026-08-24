"""Cross-cutting application utilities."""

from backend.app.core.runtime import (
    AppError,
    DegradationState,
    DependencyHealth,
    VirtualClock,
    degradation_state,
)

__all__ = [
    "AppError",
    "DegradationState",
    "DependencyHealth",
    "VirtualClock",
    "degradation_state",
]
