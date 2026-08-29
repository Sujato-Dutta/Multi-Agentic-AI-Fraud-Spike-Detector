"""Ordered transaction scoring, persistence, and online spike detection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.cache.cache_service import CacheService
from backend.app.config import Settings, get_settings
from backend.app.core.runtime import AppError, DegradationState, degradation_state
from backend.app.db.models import FraudScore, Transaction
from backend.app.db.repositories import TransactionRepository
from backend.app.db.session import SessionFactory
from backend.app.ml.fraud.predictor import ResilientFraudScorer
from backend.app.ml.spike_detection.detector import RiskDensitySpikeDetector
from backend.app.ml.spike_detection.segmentation import discover_segments
from backend.app.ml.spike_detection.windows import (
    SlidingWindowAggregator,
    WindowSnapshot,
    build_sliding_windows,
)
from backend.app.monitoring.prometheus import observe_incident, observe_transaction
from backend.app.safety.metrics import record_degradation
from backend.app.services.incident_service import IncidentService, Publisher
from backend.app.streaming.topics import TopicSet
from evaluation.dataio import load_features


@dataclass(frozen=True, slots=True)
class IngestResult:
    transaction_id: str
    created: bool
    risk_probability: float
    decision_score: float
    decision_threshold: float
    score_space: str
    degraded: bool
    degradation_reason: str | None
    emitted_windows: int
    incidents: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "created": self.created,
            "risk_probability": self.risk_probability,
            "decision_score": self.decision_score,
            "decision_threshold": self.decision_threshold,
            "score_space": self.score_space,
            "degraded": self.degraded,
            "degradation_reason": self.degradation_reason,
            "emitted_windows": self.emitted_windows,
            "incidents": list(self.incidents),
        }


class TransactionService:
    """Single-owner event-time detector service; run one instance per transaction partition."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: CacheService | None = None,
        session_factory: Any = SessionFactory,
        publisher: Publisher | None = None,
        incident_service: IncidentService | None = None,
        state: DegradationState = degradation_state,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache or CacheService(settings=self.settings, state=state)
        self.session_factory = session_factory
        self.publisher = publisher
        self.state = state
        self.topics = TopicSet.from_settings(self.settings)
        self.scorer = ResilientFraudScorer(
            self.settings.fraud_primary_model_path,
            self.settings.fraud_fallback_model_path,
        )
        self.incident_service = incident_service or IncidentService(
            self.settings,
            session_factory=session_factory,
            publisher=publisher,
        )
        self.reference_features: pd.DataFrame | None = None
        self.reference_rows: pd.DataFrame | None = None
        self.aggregator: SlidingWindowAggregator | None = None
        self.detector: RiskDensitySpikeDetector | None = None
        self.score_space: str | None = None
        self._lock = asyncio.Lock()
        self._processed = 0
        self._incident_count = 0
        self._fraud_degradation_reported = False

    async def initialize(self, reference_features: pd.DataFrame | None = None) -> None:
        self.reference_features = (
            reference_features.copy()
            if reference_features is not None
            else load_features("train", self.settings.data_dir)
        )
        self._prime_reference()

    def _prime_reference(self) -> None:
        assert self.reference_features is not None
        scores = self.scorer.score(self.reference_features)
        risk = np.asarray(scores["risk_probability"], dtype=float)
        decision = np.asarray(scores["decision_score"], dtype=float)
        threshold = float(scores["decision_threshold"])
        windows = build_sliding_windows(
            self.reference_features,
            risk,
            decision,
            threshold,
            window_minutes=self.settings.detector_window_minutes,
            slide_minutes=self.settings.detector_slide_minutes,
        )
        self.detector = RiskDensitySpikeDetector(self.settings)
        self.detector.prime(windows)
        self.aggregator = SlidingWindowAggregator(
            self.settings.detector_window_minutes,
            self.settings.detector_slide_minutes,
        )
        self.reference_rows = self.reference_features.assign(
            risk_probability=risk,
            decision_score=decision,
            high_risk=decision >= threshold,
        )
        self.score_space = str(scores["score_space"])
        if bool(scores["degraded"]):
            self.state.mark_degraded("fraud_model", str(scores["reason"]))
            if not self._fraud_degradation_reported:
                record_degradation("fraud_model_missing")
                self._fraud_degradation_reported = True
        else:
            self.state.mark_healthy("fraud_model")

    async def reset(self) -> None:
        async with self._lock:
            if self.reference_features is None:
                await self.initialize()
            else:
                self._prime_reference()
            self._processed = 0
            self._incident_count = 0

    async def ingest_batch(
        self,
        transactions: list[dict[str, Any]],
        trace_id: str | None = None,
        *,
        buffer_on_database_failure: bool = True,
    ) -> list[IngestResult]:
        """Score an ordered API batch once, persist it atomically, then advance detector state."""

        if not transactions:
            return []
        if self.aggregator is None or self.detector is None or self.reference_rows is None:
            await self.initialize()
        trace_id = trace_id or str(uuid4())
        normalized = [_normalize_transaction(item) for item in transactions]
        ids = [str(item["transaction_id"]) for item in normalized]
        if len(ids) != len(set(ids)):
            raise AppError("duplicate_batch_ids", 422, "Transaction IDs must be unique within a batch")
        for item in normalized:
            item["trace_id"] = trace_id
        frame = pd.DataFrame(
            [{key: value for key, value in item.items() if key != "trace_id"} for item in normalized]
        )
        batch = self.scorer.score(frame)
        risk = np.asarray(batch["risk_probability"], dtype=float)
        decision = np.asarray(batch["decision_score"], dtype=float)
        threshold = float(batch["decision_threshold"])
        async with self._lock:
            if str(batch["score_space"]) != self.score_space:
                self._prime_reference()
                if str(batch["score_space"]) != self.score_space:
                    raise RuntimeError("Scorer changed score space during detector re-prime")
            try:
                async with self.session_factory() as session:
                    existing = set(
                        await session.scalars(
                            select(Transaction.transaction_id).where(
                                Transaction.transaction_id.in_(ids)
                            )
                        )
                    )
                    for index, item in enumerate(normalized):
                        if ids[index] in existing:
                            continue
                        row = Transaction(**item)
                        row.score = FraudScore(
                            risk_probability=float(risk[index]),
                            decision_score=float(decision[index]),
                            decision_threshold=threshold,
                            score_space=str(batch["score_space"]),
                            degraded=bool(batch["degraded"]),
                            reason=batch.get("reason"),
                        )
                        session.add(row)
                    await session.commit()
            except SQLAlchemyError as exc:
                await self._database_unavailable(
                    normalized,
                    trace_id,
                    exc,
                    buffer=buffer_on_database_failure,
                )
                raise AssertionError("unreachable") from exc
            self.state.mark_healthy("postgres")
            results: list[IngestResult] = []
            assert self.aggregator is not None
            for index, item in enumerate(normalized):
                score: dict[str, Any] = {
                    "risk_probability": float(risk[index]),
                    "decision_score": float(decision[index]),
                    "decision_threshold": threshold,
                    "score_space": str(batch["score_space"]),
                    "degraded": bool(batch["degraded"]),
                    "reason": batch.get("reason"),
                }
                if ids[index] in existing:
                    results.append(
                        IngestResult(
                            transaction_id=ids[index],
                            created=False,
                            risk_probability=score["risk_probability"],
                            decision_score=score["decision_score"],
                            decision_threshold=threshold,
                            score_space=score["score_space"],
                            degraded=score["degraded"],
                            degradation_reason=score["reason"],
                            emitted_windows=0,
                            incidents=(),
                        )
                    )
                    continue
                await self.cache.claim_transaction(
                    ids[index],
                    ttl_seconds=self.settings.transaction_claim_ttl_seconds,
                )
                await self.cache.set_prediction(ids[index], score, ttl_seconds=86_400)
                windows = self.aggregator.add(
                    item,
                    risk_probability=score["risk_probability"],
                    decision_score=score["decision_score"],
                    decision_threshold=threshold,
                )
                incidents = await self._process_windows(windows, trace_id)
                self._processed += 1
                results.append(
                    IngestResult(
                        transaction_id=ids[index],
                        created=True,
                        risk_probability=score["risk_probability"],
                        decision_score=score["decision_score"],
                        decision_threshold=threshold,
                        score_space=score["score_space"],
                        degraded=score["degraded"],
                        degradation_reason=score["reason"],
                        emitted_windows=len(windows),
                        incidents=tuple(incidents),
                    )
                )
            for item in results:
                _observe(item)
            return results

    async def ingest(
        self,
        transaction: dict[str, Any],
        trace_id: str | None = None,
        *,
        buffer_on_database_failure: bool = True,
    ) -> IngestResult:
        result = await self._ingest_one(
            transaction, trace_id, buffer_on_database_failure=buffer_on_database_failure
        )
        _observe(result)
        return result

    async def _ingest_one(
        self,
        transaction: dict[str, Any],
        trace_id: str | None = None,
        *,
        buffer_on_database_failure: bool = True,
    ) -> IngestResult:
        if self.aggregator is None or self.detector is None or self.reference_rows is None:
            await self.initialize()
        normalized = _normalize_transaction(transaction)
        transaction_id = str(normalized["transaction_id"])
        trace_id = trace_id or str(uuid4())
        normalized["trace_id"] = trace_id
        claimed = await self.cache.claim_transaction(
            transaction_id,
            ttl_seconds=self.settings.transaction_claim_ttl_seconds,
        )
        async with self._lock:
            if not claimed:
                stored = await self._stored_result(transaction_id)
                if stored is not None:
                    return stored
                claimed = await self.cache.claim_transaction(
                    transaction_id,
                    ttl_seconds=self.settings.transaction_claim_ttl_seconds,
                )
                if not claimed:
                    raise AppError("transaction_in_progress", 409, "Transaction is already processing")
            try:
                frame = pd.DataFrame([{key: value for key, value in normalized.items() if key != "trace_id"}])
                batch = self.scorer.score(frame)
                score = _scalar_score(batch)
                if str(score["score_space"]) != self.score_space:
                    self._prime_reference()
                    if str(score["score_space"]) != self.score_space:
                        raise RuntimeError("Scorer changed score space during detector re-prime")
                try:
                    async with self.session_factory() as session:
                        row, score_row, created = await TransactionRepository(
                            session
                        ).insert_with_score(
                            normalized,
                            score,
                        )
                        await session.commit()
                except SQLAlchemyError as exc:
                    await self._database_unavailable(
                        [normalized],
                        trace_id,
                        exc,
                        buffer=buffer_on_database_failure,
                    )
                    raise AssertionError("unreachable") from exc
                self.state.mark_healthy("postgres")
                if not created:
                    return _stored_ingest_result(row.transaction_id, score_row)
                await self.cache.set_prediction(transaction_id, score, ttl_seconds=86_400)
                if self.publisher is not None:
                    await self.publisher.send(
                        self.topics.fraud_scores,
                        "fraud.scored",
                        {"transaction_id": transaction_id, **score},
                        trace_id=trace_id,
                        key=transaction_id,
                    )
                try:
                    windows = self.aggregator.add(
                        normalized,
                        risk_probability=float(score["risk_probability"]),
                        decision_score=float(score["decision_score"]),
                        decision_threshold=float(score["decision_threshold"]),
                    )
                except ValueError as exc:
                    raise AppError("event_time_out_of_order", 409, str(exc)) from exc
                incidents = await self._process_windows(windows, trace_id)
                self._processed += 1
                return IngestResult(
                    transaction_id=transaction_id,
                    created=True,
                    risk_probability=float(score["risk_probability"]),
                    decision_score=float(score["decision_score"]),
                    decision_threshold=float(score["decision_threshold"]),
                    score_space=str(score["score_space"]),
                    degraded=bool(score["degraded"]),
                    degradation_reason=score.get("reason"),
                    emitted_windows=len(windows),
                    incidents=tuple(incidents),
                )
            except Exception:
                await self.cache.release_transaction(transaction_id)
                raise

    async def flush(self, trace_id: str | None = None) -> list[dict[str, Any]]:
        if self.aggregator is None:
            return []
        async with self._lock:
            return await self._process_windows(
                self.aggregator.flush(), trace_id or str(uuid4())
            )

    async def _process_windows(
        self, windows: list[WindowSnapshot], trace_id: str
    ) -> list[dict[str, Any]]:
        assert self.detector is not None and self.reference_rows is not None
        incidents: list[dict[str, Any]] = []
        for window in windows:
            alert = self.detector.process(window)
            await self.cache.set_detector_state(
                {
                    "score_space": self.score_space,
                    "baseline_density": self.detector.baseline_density,
                    "active": self.detector.active,
                    "last_window": window.to_dict(),
                    "processed_transactions": self._processed,
                },
                ttl_seconds=86_400,
            )
            if alert is None:
                continue
            findings = discover_segments(
                window.rows,
                self.reference_rows,
                min_support=min(10, max(5, window.transaction_count // 4)),
                max_depth=3,
                top_k=5,
            )
            payload, created = await self.incident_service.create_from_alert(
                alert,
                [finding.to_dict() for finding in findings],
                window.rows,
                trace_id=trace_id,
            )
            if created:
                incidents.append(payload)
                observe_incident(payload)
                self._incident_count += 1
        if len(self.detector.decisions) > 2_000:
            self.detector.decisions[:] = self.detector.decisions[-2_000:]
        return incidents

    async def verify_replay_equivalence(self, features: pd.DataFrame) -> list[dict[str, Any]]:
        """Run a full frame through the online-owned state without persistence side effects."""

        if self.reference_features is None:
            await self.initialize()
        async with self._lock:
            self._prime_reference()
            batch = self.scorer.score(features)
            if str(batch["score_space"]) != self.score_space:
                self._prime_reference()
            risk = np.asarray(batch["risk_probability"], dtype=float)
            decision = np.asarray(batch["decision_score"], dtype=float)
            threshold = float(batch["decision_threshold"])
            assert self.aggregator is not None and self.detector is not None
            alerts: list[dict[str, Any]] = []
            for (_, row), probability, score in zip(
                features.iterrows(), risk, decision, strict=True
            ):
                windows = self.aggregator.add(
                    row.to_dict(),
                    risk_probability=float(probability),
                    decision_score=float(score),
                    decision_threshold=threshold,
                )
                for window in windows:
                    alert = self.detector.process(window)
                    if alert is not None:
                        alerts.append(alert.to_dict())
            for window in self.aggregator.flush():
                alert = self.detector.process(window)
                if alert is not None:
                    alerts.append(alert.to_dict())
            return alerts

    async def _stored_result(self, transaction_id: str) -> IngestResult | None:
        async with self.session_factory() as session:
            row = await TransactionRepository(session).get(transaction_id)
            if row is None or row.score is None:
                return None
            return _stored_ingest_result(row.transaction_id, row.score)

    async def _database_unavailable(
        self,
        transactions: list[dict[str, Any]],
        trace_id: str,
        exc: SQLAlchemyError,
        *,
        buffer: bool,
    ) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        self.state.mark_down("postgres", reason)
        record_degradation("postgres_down")
        buffered = 0
        if buffer and self.publisher is not None:
            for transaction in transactions:
                try:
                    await self.publisher.send(
                        self.topics.transactions,
                        "transaction.buffered",
                        transaction,
                        trace_id=trace_id,
                        key=str(transaction["transaction_id"]),
                    )
                except AppError:
                    break
                buffered += 1
        raise AppError(
            "database_unavailable",
            503,
            f"Postgres write failed; {buffered} transaction(s) buffered to stream",
        ) from exc

    async def handle_stream_event(
        self, payload: dict[str, Any], trace_id: str
    ) -> IngestResult:
        return await self.ingest(
            payload, trace_id, buffer_on_database_failure=False
        )

    def stats(self) -> dict[str, Any]:
        return {
            "processed_transactions": self._processed,
            "incidents_created": self._incident_count,
            "score_space": self.score_space,
            "model_degraded": self.scorer.degraded,
            "detector_active": self.detector.active if self.detector else False,
            "baseline_density": self.detector.baseline_density if self.detector else None,
        }


def _observe(result: IngestResult) -> None:
    observe_transaction(
        created=result.created,
        risk_probability=result.risk_probability,
        decision_score=result.decision_score,
        threshold=result.decision_threshold,
    )


def _scalar_score(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "risk_probability": float(np.asarray(batch["risk_probability"]).reshape(-1)[0]),
        "decision_score": float(np.asarray(batch["decision_score"]).reshape(-1)[0]),
        "decision_threshold": float(batch["decision_threshold"]),
        "score_space": str(batch["score_space"]),
        "degraded": bool(batch["degraded"]),
        "reason": batch.get("reason"),
    }


def _normalize_transaction(values: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, pd.Timestamp):
            normalized[key] = value.to_pydatetime()
        elif hasattr(value, "item"):
            normalized[key] = value.item()
        elif pd.isna(value):
            normalized[key] = None
        else:
            normalized[key] = value
    return normalized


def _stored_ingest_result(transaction_id: str, score: Any) -> IngestResult:
    return IngestResult(
        transaction_id=transaction_id,
        created=False,
        risk_probability=float(score.risk_probability),
        decision_score=float(score.decision_score),
        decision_threshold=float(score.decision_threshold),
        score_space=str(score.score_space),
        degraded=bool(score.degraded),
        degradation_reason=score.reason,
        emitted_windows=0,
        incidents=(),
    )
