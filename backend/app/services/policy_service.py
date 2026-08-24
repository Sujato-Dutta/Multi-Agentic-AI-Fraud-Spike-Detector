"""Human-gated policy lifecycle and registry-backed runtime resolution."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson

from backend.app.config import Settings, get_settings
from backend.app.core.runtime import AppError, DegradationState
from backend.app.db.models import PolicyVersion
from backend.app.db.repositories import AuditRepository, LearningRepository
from backend.app.ml.policy.contextual_bandit import LinUCBPolicy
from backend.app.ml.policy.shadow_policy import (
    PolicyMetrics,
    PromotionGate,
    ShadowPolicy,
)
from backend.app.safety.permissions import is_action_allowed
from backend.app.schemas import UserIdentity
from backend.app.services.evaluation_service import ActionEffects

EVALUATION_SCHEMA = "policy-evaluation-v1"
BUILTIN_CONSERVATIVE_IDENTITY = "builtin:conservative-human-escalation-v1"


def build_promotion_evidence(
    *,
    candidate_metrics: Mapping[str, Any],
    production_metrics: Mapping[str, Any],
    holdback: Mapping[str, Any],
    assumptions_version: str,
    candidate_checksum: str,
    production_identity: str,
    candidate_version: int | None = None,
    production_version: int | None = None,
) -> dict[str, Any]:
    """Create immutable same-holdback evidence bound to exact artifact identities."""

    holdback_payload = dict(holdback)
    evidence = {
        "schema_version": EVALUATION_SCHEMA,
        "evaluator_version": "phase6-shadow-evaluator-v1",
        "assumptions_version": assumptions_version,
        "holdback": holdback_payload,
        "holdback_digest": _digest(holdback_payload),
        "candidate_artifact_checksum": candidate_checksum,
        "production_identity": production_identity,
        "candidate_version": candidate_version,
        "production_version": production_version,
        "candidate_metrics": dict(candidate_metrics),
        "production_metrics": dict(production_metrics),
    }
    evidence["evidence_digest"] = _digest(evidence)
    return evidence


class RuntimePolicyResolver:
    """Resolve the DB-active policy for every incident; cache only verified models."""

    def __init__(
        self,
        *,
        session_factory: Any,
        assumptions_version: str,
        state: DegradationState | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.assumptions_version = assumptions_version
        self.state = state
        self._cache: dict[tuple[int, str], LinUCBPolicy] = {}

    async def validate_active(self) -> tuple[bool, str | None]:
        try:
            async with self.session_factory() as session:
                active = await LearningRepository(session).active_policy_version()
            if active is None:
                return False, "active_policy_missing"
            _, error, builtin = await self._load(active)
            return (builtin or error is None), error
        except Exception as exc:  # noqa: BLE001 - health must report, not crash startup
            return False, f"{type(exc).__name__}: {exc}"[:500]

    async def score(self, context: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with self.session_factory() as session:
                repository = LearningRepository(session)
                active = await repository.active_policy_version()
                rows = await repository.list_policy_versions()
            candidate = next(
                (row for row in rows if row.status in {"candidate", "shadow"}), None
            )
        except Exception as exc:  # noqa: BLE001 - DB failure uses conservative response
            return self._fallback(f"registry:{type(exc).__name__}: {exc}"[:500])

        production, production_error, builtin = await self._load(active)
        candidate_model, candidate_error, _ = await self._load(candidate)
        result = ShadowPolicy(
            production,
            candidate_model,
            production_error=production_error,
            candidate_error=candidate_error,
        ).score(context)
        if builtin:
            result["degraded"] = False
            result["production_error"] = None
        result.update(
            {
                "production_policy_version_id": (
                    active.policy_version_id if active is not None else None
                ),
                "production_identity": (
                    _artifact_identity(active) if active is not None else None
                ),
                "candidate_policy_version_id": (
                    candidate.policy_version_id if candidate is not None else None
                ),
            }
        )
        if self.state is not None:
            if result["degraded"]:
                self.state.mark_degraded(
                    "response_policy", str(result.get("production_error") or "fallback")
                )
            else:
                self.state.mark_healthy("response_policy")
        return result

    async def _load(
        self, row: PolicyVersion | None
    ) -> tuple[LinUCBPolicy | None, str | None, bool]:
        if row is None:
            return None, "active_policy_missing", False
        if _is_builtin(row):
            return None, None, True
        if not row.artifact_uri or not row.artifact_checksum:
            return None, "artifact_identity_missing", False
        key = (row.policy_version_id, row.artifact_checksum)
        if key in self._cache:
            return self._cache[key], None, False
        try:
            model = await asyncio.to_thread(
                LinUCBPolicy.load,
                Path(row.artifact_uri),
                assumptions_version=self.assumptions_version,
                expected_checksum=row.artifact_checksum,
            )
        except Exception as exc:  # noqa: BLE001 - corrupt artifacts fail closed
            return None, f"{type(exc).__name__}: {exc}"[:500], False
        self._cache[key] = model
        return model, None, False

    def _fallback(self, error: str) -> dict[str, Any]:
        result = ShadowPolicy(None, production_error=error).score({})
        result.update(
            {
                "production_policy_version_id": None,
                "production_identity": None,
                "candidate_policy_version_id": None,
            }
        )
        if self.state is not None:
            self.state.mark_degraded("response_policy", error)
        return result


class PolicyService:
    def __init__(self, *, session_factory: Any, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        config = settings or get_settings()
        self.assumptions_version = ActionEffects.from_yaml(
            config.action_effects_path
        ).version
        self.gate = PromotionGate(
            reward_margin_inr=config.policy_reward_margin_inr,
            recall_tolerance=config.policy_recall_tolerance,
            fp_cost_tolerance=config.policy_fp_cost_tolerance,
        )

    async def list_versions(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            repository = LearningRepository(session)
            active = await repository.active_policy_version()
            rows = await repository.list_policy_versions()
        return {
            "active_policy_version_id": (
                active.policy_version_id if active is not None else None
            ),
            "items": [_version_dict(row) for row in rows],
        }

    async def comparison(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            repository = LearningRepository(session)
            production = await repository.active_policy_version()
            rows = await repository.list_policy_versions()
        candidate = next(
            (row for row in rows if row.status in {"candidate", "shadow"}), None
        )
        gate = None
        evidence_status = "missing"
        if production is not None and candidate is not None:
            try:
                candidate_metrics, production_metrics, _ = self._evidence(
                    candidate, production
                )
                gate = self.gate.evaluate(
                    candidate_metrics, production_metrics
                ).model_dump(mode="json")
                evidence_status = "valid"
            except AppError as exc:
                evidence_status = exc.code
        return {
            "production": _version_dict(production) if production is not None else None,
            "candidate": _version_dict(candidate) if candidate is not None else None,
            "promotion_gate": gate,
            "promotion_evidence": evidence_status,
            "automatic_promotion": False,
        }

    async def promote(self, policy_version_id: int, actor: UserIdentity) -> dict[str, Any]:
        self._require_admin(actor, "promote_policy")
        async with self.session_factory() as session:
            repository = LearningRepository(session)
            candidate = await repository.get_policy_version(policy_version_id)
            production = await repository.active_policy_version()
            if candidate is None:
                raise AppError("policy_version_not_found", 404, "Candidate version does not exist")
            if candidate.status not in {"candidate", "shadow"}:
                raise AppError("policy_not_candidate", 409, "Only a shadow candidate can promote")
            if production is None:
                raise AppError(
                    "production_policy_missing",
                    409,
                    "A measured production baseline is required before promotion",
                )
            candidate_metrics, production_metrics, evidence = self._evidence(
                candidate, production
            )
            await self._verify_artifact(candidate)
            await self._verify_artifact(production)
            gate = self.gate.evaluate(candidate_metrics, production_metrics)
            candidate.gate_result = gate.model_dump(mode="json")
            if not gate.passed:
                await session.commit()
                raise AppError("promotion_gate_failed", 409, ", ".join(gate.reasons))
            try:
                promoted, previous = await repository.activate_version(
                    policy_version_id,
                    actor.username,
                    expected_active_policy_version_id=production.policy_version_id,
                )
            except ValueError as exc:
                raise AppError("stale_policy_evaluation", 409, str(exc)) from exc
            await AuditRepository(session).append_once(
                incident_id=None,
                event_type="response_policy_promoted",
                actor=actor.username,
                payload={
                    "policy_version_id": promoted.policy_version_id,
                    "version": promoted.version,
                    "previous_version": previous.version if previous else None,
                    "candidate_artifact_checksum": promoted.artifact_checksum,
                    "production_identity": evidence["production_identity"],
                    "holdback_digest": evidence["holdback_digest"],
                    "evidence_digest": evidence["evidence_digest"],
                    "gate_result": gate.model_dump(mode="json"),
                },
                trace_id=None,
                idempotency_key=f"policy-promote:{promoted.policy_version_id}",
            )
            await session.commit()
            return _version_dict(promoted)

    async def rollback(self, policy_version_id: int, actor: UserIdentity) -> dict[str, Any]:
        self._require_admin(actor, "rollback_policy")
        async with self.session_factory() as session:
            repository = LearningRepository(session)
            target = await repository.get_policy_version(policy_version_id)
            current = await repository.active_policy_version()
            if target is None:
                raise AppError("policy_version_not_found", 404, "Rollback version does not exist")
            if target.status != "retired":
                raise AppError("policy_not_retired", 409, "Rollback target must be a retired version")
            if current is None:
                raise AppError("production_policy_missing", 409, "No active policy to replace")
            await self._verify_artifact(target)
            try:
                restored, previous = await repository.activate_version(
                    policy_version_id,
                    actor.username,
                    expected_active_policy_version_id=current.policy_version_id,
                )
            except ValueError as exc:
                raise AppError("stale_policy_activation", 409, str(exc)) from exc
            await AuditRepository(session).append_once(
                incident_id=None,
                event_type="response_policy_rolled_back",
                actor=actor.username,
                payload={
                    "policy_version_id": restored.policy_version_id,
                    "version": restored.version,
                    "artifact_identity": _artifact_identity(restored),
                    "replaced_version": previous.version if previous else None,
                },
                trace_id=None,
                idempotency_key=(
                    f"policy-rollback:{restored.policy_version_id}:"
                    f"{previous.policy_version_id if previous else 0}"
                ),
            )
            await session.commit()
            return _version_dict(restored)

    def _evidence(
        self, candidate: PolicyVersion, production: PolicyVersion
    ) -> tuple[PolicyMetrics, PolicyMetrics, dict[str, Any]]:
        evidence = candidate.metrics.get("promotion_evidence")
        if not isinstance(evidence, dict):
            raise AppError(
                "promotion_evidence_missing", 409, "Bound holdback evidence is required"
            )
        supplied_digest = evidence.get("evidence_digest")
        unsigned = {key: value for key, value in evidence.items() if key != "evidence_digest"}
        checks = {
            "schema": evidence.get("schema_version") == EVALUATION_SCHEMA,
            "evidence_digest": supplied_digest == _digest(unsigned),
            "holdback_digest": evidence.get("holdback_digest")
            == _digest(evidence.get("holdback", {})),
            "assumptions_version": evidence.get("assumptions_version")
            == self.assumptions_version,
            "candidate_checksum": evidence.get("candidate_artifact_checksum")
            == candidate.artifact_checksum,
            "production_identity": evidence.get("production_identity")
            == _artifact_identity(production),
            "candidate_version": evidence.get("candidate_version") in {
                None,
                candidate.version,
            },
            "production_version": evidence.get("production_version") in {
                None,
                production.version,
            },
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise AppError(
                "promotion_evidence_stale", 409, f"Invalid promotion evidence: {', '.join(failed)}"
            )
        try:
            return (
                PolicyMetrics.model_validate(evidence["candidate_metrics"]),
                PolicyMetrics.model_validate(evidence["production_metrics"]),
                evidence,
            )
        except (KeyError, ValueError) as exc:
            raise AppError(
                "promotion_evidence_invalid", 409, "Promotion metrics are invalid"
            ) from exc

    async def _verify_artifact(self, row: PolicyVersion) -> None:
        if _is_builtin(row):
            return
        if not row.artifact_uri or not row.artifact_checksum:
            raise AppError(
                "policy_artifact_missing", 409, "Policy artifact URI and checksum are required"
            )
        try:
            await asyncio.to_thread(
                LinUCBPolicy.load,
                Path(row.artifact_uri),
                assumptions_version=self.assumptions_version,
                expected_checksum=row.artifact_checksum,
            )
        except Exception as exc:
            raise AppError(
                "policy_artifact_invalid", 409, f"Policy artifact validation failed: {exc}"
            ) from exc

    @staticmethod
    def _require_admin(actor: UserIdentity, action: str) -> None:
        if actor.role != "admin" or not is_action_allowed(actor.role, action):
            raise AppError("forbidden_policy_change", 403, "Admin approval is required")


def _is_builtin(row: PolicyVersion) -> bool:
    return (
        row.rules.get("family") == "fixed_human_escalation"
        and row.artifact_uri is None
        and row.artifact_checksum in {None, BUILTIN_CONSERVATIVE_IDENTITY}
    )


def _artifact_identity(row: PolicyVersion) -> str:
    if _is_builtin(row):
        return BUILTIN_CONSERVATIVE_IDENTITY
    return str(row.artifact_checksum or "missing")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _version_dict(row: Any) -> dict[str, Any]:
    return {
        "policy_version_id": row.policy_version_id,
        "policy_id": row.policy_id,
        "version": row.version,
        "status": row.status,
        "rules": row.rules,
        "artifact_uri": row.artifact_uri,
        "artifact_checksum": row.artifact_checksum,
        "metrics": row.metrics,
        "gate_result": row.gate_result,
        "parent_version": row.parent_version,
        "created_by": row.created_by,
        "approved_by": row.approved_by,
        "created_at": row.created_at.isoformat(),
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
    }
