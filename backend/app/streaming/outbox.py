"""Durable outbox dispatcher; Postgres and Kafka remain at-least-once."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from uuid import uuid4

import structlog

from backend.app.config import Settings, get_settings
from backend.app.core.runtime import DegradationState, degradation_state
from backend.app.db.repositories import OutboxRepository
from backend.app.streaming.producer import EventProducer
from backend.app.streaming.topics import EventEnvelope

logger = structlog.get_logger(__name__)


class OutboxDispatcher:
    def __init__(
        self,
        *,
        session_factory: Any,
        producer: EventProducer,
        settings: Settings | None = None,
        state: DegradationState = degradation_state,
    ) -> None:
        self.session_factory = session_factory
        self.producer = producer
        self.settings = settings or get_settings()
        self.state = state
        self.worker_id = f"outbox-{uuid4()}"
        self._stop = asyncio.Event()

    async def drain_once(self) -> int:
        async with self.session_factory() as session:
            rows = await OutboxRepository(session).claim_batch(
                worker_id=self.worker_id,
                limit=self.settings.outbox_batch_size,
                lease_seconds=self.settings.outbox_lease_seconds,
            )
            await session.commit()
        self.state.mark_healthy("postgres")
        published = 0
        for row in rows:
            try:
                envelope = EventEnvelope(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    occurred_at=row.occurred_at,
                    trace_id=row.trace_id,
                    payload=row.payload,
                )
                await asyncio.wait_for(
                    self.producer.send_envelope(
                        row.topic, envelope, key=row.message_key
                    ),
                    timeout=self.settings.outbox_publish_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - durable row remains retryable
                async with self.session_factory() as session:
                    await OutboxRepository(session).mark_failed(
                        row.event_id,
                        self.worker_id,
                        f"{type(exc).__name__}: {exc}",
                        row.attempts,
                    )
                    await session.commit()
                self.state.mark_healthy("postgres")
                logger.warning(
                    "outbox_publish_failed", event_id=row.event_id, reason=str(exc)[:500]
                )
            else:
                async with self.session_factory() as session:
                    await OutboxRepository(session).mark_published(
                        row.event_id, self.worker_id
                    )
                    await session.commit()
                self.state.mark_healthy("postgres")
                published += 1
        return published

    async def run(self) -> None:
        consecutive_failures = 0
        last_logged_reason: str | None = None
        last_logged_at = 0.0
        while not self._stop.is_set():
            delay = self.settings.outbox_poll_seconds
            try:
                await self.drain_once()
            except Exception as exc:  # noqa: BLE001 - next cycle retries durable rows
                consecutive_failures += 1
                reason = f"{type(exc).__name__}: {exc}"[:500]
                self.state.mark_down("postgres", reason)
                delay = self._retry_delay(consecutive_failures)
                now = time.monotonic()
                if (
                    reason != last_logged_reason
                    or now - last_logged_at
                    >= self.settings.outbox_cycle_log_interval_seconds
                ):
                    logger.warning(
                        "outbox_cycle_failed",
                        dependency="postgres",
                        reason=reason,
                        consecutive_failures=consecutive_failures,
                        retry_seconds=round(delay, 3),
                    )
                    last_logged_reason = reason
                    last_logged_at = now
            else:
                if consecutive_failures:
                    logger.info(
                        "outbox_cycle_recovered",
                        dependency="postgres",
                        failed_cycles=consecutive_failures,
                    )
                consecutive_failures = 0
                last_logged_reason = None
                last_logged_at = 0.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _retry_delay(self, consecutive_failures: int) -> float:
        exponent = min(consecutive_failures - 1, 20)
        ceiling = min(
            self.settings.outbox_cycle_retry_max_seconds,
            self.settings.outbox_poll_seconds * (2**exponent),
        )
        return random.uniform(ceiling * 0.8, ceiling)

    async def stop(self) -> None:
        self._stop.set()
