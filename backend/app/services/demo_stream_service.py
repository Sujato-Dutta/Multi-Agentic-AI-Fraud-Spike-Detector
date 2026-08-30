"""Bounded, local-only transaction replay used by the browser demo control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import pandas as pd
import structlog
from sqlalchemy import select

from backend.app.cache import keys
from backend.app.cache.cache_service import CacheService
from backend.app.config import Settings, get_settings
from backend.app.core.runtime import AppError, VirtualClock
from backend.app.core.security import LOCAL_DEMO_ENVS
from backend.app.db.models import Transaction
from backend.app.db.session import SessionFactory
from backend.app.streaming.topics import TopicSet
from evaluation.dataio import DatasetSplit, load_split

logger = structlog.get_logger(__name__)

DEMO_SCENARIO = "validation_spike_val_s1"
DEMO_EVENT_ID = "VAL_S1"
DEMO_SPEED = 600.0
DEMO_MARKER_TTL_SECONDS = 31_536_000
MAX_DEMO_TRANSACTIONS = 10_000


class DemoPublisher(Protocol):
    async def send(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str,
        key: str | None = None,
    ) -> object: ...


class DemoStreamService:
    """Publish one reviewed demo fixture without exposing commands or file paths."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        producer: DemoPublisher | None,
        cache: CacheService,
        session_factory: Any = SessionFactory,
        runtime_reset: Callable[[], Awaitable[None]] | None = None,
        split_loader: Any = load_split,
        speed: float = DEMO_SPEED,
    ) -> None:
        self.settings = settings or get_settings()
        self.producer = producer
        self.cache = cache
        self.session_factory = session_factory
        self.runtime_reset = runtime_reset
        self.split_loader = split_loader
        self.speed = speed
        self.topics = TopicSet.from_settings(self.settings)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._snapshot = self._idle_snapshot()

    async def start(self, scenario: str) -> dict[str, Any]:
        self._ensure_local()
        if scenario != DEMO_SCENARIO:
            raise AppError("demo_scenario_invalid", 422, "Unsupported demo scenario")
        if self.producer is None:
            raise AppError(
                "demo_stream_unavailable",
                503,
                "The Redpanda producer is unavailable; check the dependency strip",
            )

        async with self._lock:
            state = self._snapshot["state"]
            if state in {"queued", "running"}:
                raise AppError("demo_stream_running", 409, "The demo stream is already running")
            frame = await asyncio.to_thread(self._select_demo_rows)
            if await self._has_persisted_demo_rows(frame):
                raise self._reset_required()
            run_id = str(uuid4())
            now = _now()
            claimed = await self.cache.claim_json(
                keys.demo_stream_key(DEMO_SCENARIO),
                {"run_id": run_id, "scenario": DEMO_SCENARIO, "claimed_at": now},
                ttl_seconds=DEMO_MARKER_TTL_SECONDS,
            )
            if claimed is None:
                raise AppError(
                    "demo_stream_unavailable",
                    503,
                    "Redis must confirm the demo replay claim; check dependency health",
                )
            if not claimed:
                raise self._reset_required()
            if state in {"completed", "failed"} and self.runtime_reset is not None:
                await self.runtime_reset()
            self._snapshot = {
                "run_id": run_id,
                "scenario": DEMO_SCENARIO,
                "event_id": DEMO_EVENT_ID,
                "state": "queued",
                "published": 0,
                "total": len(frame),
                "percent": 0.0,
                "started_at": now,
                "updated_at": now,
                "completed_at": None,
                "error": None,
            }
            self._task = asyncio.create_task(
                self._run(run_id, frame), name=f"demo-stream-{run_id}"
            )
            return dict(self._snapshot)

    async def status(self) -> dict[str, Any]:
        self._ensure_local()
        async with self._lock:
            return dict(self._snapshot)

    async def close(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self, run_id: str, frame: pd.DataFrame) -> None:
        try:
            await self._update(
                run_id,
                state="running",
                updated_at=_now(),
            )
            first_timestamp = frame.iloc[0]["timestamp"].to_pydatetime()
            previous_timestamp = first_timestamp
            clock = VirtualClock(speed=self.speed, start=first_timestamp)
            for position, (_, row) in enumerate(frame.iterrows(), start=1):
                timestamp = row["timestamp"].to_pydatetime()
                if position > 1:
                    await clock.wait(timestamp - previous_timestamp)
                previous_timestamp = timestamp
                payload = {key: _json_value(value) for key, value in row.to_dict().items()}
                assert self.producer is not None
                await self.producer.send(
                    self.topics.transactions,
                    "transaction.received",
                    payload,
                    trace_id=run_id,
                    key=str(row["transaction_id"]),
                )
                await self._update(
                    run_id,
                    published=position,
                    percent=round(100 * position / len(frame), 1),
                    updated_at=_now(),
                )
            now = _now()
            await self._update(
                run_id,
                state="completed",
                percent=100.0,
                updated_at=now,
                completed_at=now,
            )
        except asyncio.CancelledError:
            await self._update(
                run_id,
                state="failed",
                updated_at=_now(),
                completed_at=_now(),
                error={"code": "demo_stream_cancelled", "detail": "Demo stream was cancelled"},
            )
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "demo_stream_failed"
            detail = (
                exc.detail
                if isinstance(exc, AppError)
                else "The demo stream failed; check API logs and dependency health"
            )
            logger.exception("demo_stream_failed", run_id=run_id, reason=str(exc)[:500])
            now = _now()
            await self._update(
                run_id,
                state="failed",
                updated_at=now,
                completed_at=now,
                error={"code": code, "detail": detail},
            )

    async def _has_persisted_demo_rows(self, frame: pd.DataFrame) -> bool:
        transaction_ids = [str(value) for value in frame["transaction_id"].tolist()]
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(Transaction.transaction_id)
                .where(Transaction.transaction_id.in_(transaction_ids))
                .limit(1)
            )
        return existing is not None

    @staticmethod
    def _reset_required() -> AppError:
        return AppError(
            "demo_stream_requires_reset",
            409,
            "This scenario was already claimed; run reset_demo.py before replaying it",
        )

    def _select_demo_rows(self) -> pd.DataFrame:
        split: DatasetSplit = self.split_loader("validation", self.settings.data_dir)
        event = split.spike_events.loc[split.spike_events["event_id"].eq(DEMO_EVENT_ID)]
        if event.empty:
            raise AppError("demo_fixture_missing", 503, "The VAL_S1 demo fixture is unavailable")
        selected = event.iloc[0]
        start = selected["start_timestamp"] - pd.Timedelta(hours=3)
        end = selected["end_timestamp"] + pd.Timedelta(hours=1)
        frame = (
            split.features.loc[
                split.features["timestamp"].between(start, end, inclusive="both")
            ]
            .sort_values(["timestamp", "transaction_id"], kind="stable")
            .reset_index(drop=True)
        )
        if frame.empty:
            raise AppError("demo_fixture_empty", 503, "The VAL_S1 demo fixture contains no rows")
        if len(frame) > MAX_DEMO_TRANSACTIONS:
            raise AppError(
                "demo_fixture_too_large",
                503,
                "The demo fixture exceeds the safe transaction limit",
            )
        return frame

    async def _update(self, run_id: str, **patch: Any) -> None:
        async with self._lock:
            if self._snapshot.get("run_id") != run_id:
                return
            self._snapshot = {**self._snapshot, **patch}

    def _ensure_local(self) -> None:
        if self.settings.app_env.lower() not in LOCAL_DEMO_ENVS:
            raise AppError("not_found", 404, "Not found")

    @staticmethod
    def _idle_snapshot() -> dict[str, Any]:
        return {
            "run_id": None,
            "scenario": DEMO_SCENARIO,
            "event_id": DEMO_EVENT_ID,
            "state": "idle",
            "published": 0,
            "total": None,
            "percent": 0.0,
            "started_at": None,
            "updated_at": None,
            "completed_at": None,
            "error": None,
        }


def _json_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
