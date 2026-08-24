"""Pure escalation thresholds for policy outcomes."""

from typing import Literal

Escalation = Literal["auto_handle", "require_approval", "escalate"]


def determine_escalation(
    *,
    impact_inr: float,
    confidence_score: float,
    grounding_score: float,
    segment_breadth: float,
    novelty_score: float,
    high_impact_inr: float,
    low_grounding_score: float,
    broad_segment_ratio: float,
    high_novelty_score: float,
) -> Escalation:
    """Return the most conservative deterministic handling tier."""

    if (
        impact_inr >= high_impact_inr
        or grounding_score < low_grounding_score
        or novelty_score >= high_novelty_score
    ):
        return "escalate"
    if confidence_score < 1.0 or segment_breadth >= broad_segment_ratio:
        return "require_approval"
    return "auto_handle"
