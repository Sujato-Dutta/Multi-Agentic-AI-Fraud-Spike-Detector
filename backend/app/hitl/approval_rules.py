"""Deterministically map analyst choices to final actions."""

from collections.abc import Mapping
from typing import Any

from backend.app.core.runtime import AppError


def resolve_final_action(
    decision: str,
    recommendation: Mapping[str, Any],
    modified_action: str | None = None,
) -> str:
    """Return the requested action; authorization is evaluated separately."""

    if decision == "approve":
        action = str(recommendation.get("action", ""))
    elif decision == "modify":
        action = modified_action or ""
    elif decision == "reject":
        action = "no_action"
    elif decision == "escalate":
        action = "human_escalation"
    else:
        raise AppError("invalid_review_decision", 422, "Unsupported analyst decision")
    if not action:
        raise AppError("invalid_final_action", 422, "A final action is required")
    return action
