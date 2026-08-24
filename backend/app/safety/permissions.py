"""Role-to-action permissions enforced independently of API routes."""

from typing import Literal

Action = Literal[
    "no_action",
    "enhanced_monitoring",
    "step_up_verification",
    "manual_review",
    "human_escalation",
    "temporary_defensive_rule",
    "promote_policy",
    "rollback_policy",
]
Role = Literal["analyst", "lead_analyst", "admin"]

_ANALYST_ACTIONS = frozenset(
    {
        "no_action",
        "enhanced_monitoring",
        "step_up_verification",
        "manual_review",
        "human_escalation",
    }
)
_ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "analyst": _ANALYST_ACTIONS,
    "lead_analyst": _ANALYST_ACTIONS | {"temporary_defensive_rule"},
    "admin": _ANALYST_ACTIONS
    | {"temporary_defensive_rule", "promote_policy", "rollback_policy"},
}


def is_action_allowed(role: str, action: str) -> bool:
    """Default-deny unknown roles and actions."""

    return action in _ROLE_ACTIONS.get(role, frozenset())


def allowed_actions(role: str) -> tuple[str, ...]:
    return tuple(sorted(_ROLE_ACTIONS.get(role, frozenset())))
