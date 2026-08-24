"""Resilient Redpanda event producer."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError
from prometheus_client import Counter

from backend.app.config import Settings, get_settings
from backend.app.core.runtime import AppError, DegradationState, degradation_state
from backend.app.streaming.topics import EventEnvelope, TopicSet

logger = structlog.get_logger(__name__)
STREAM_PUBLISHED = Counter(
    "fraud_stream_published_total", "Events published to Redpanda.", ("topic", "event_type")
)
STREAM_PUBLISH_FAILURES = Counter(
    "fraud_stream_publish_failures_total", "Failed Redpanda publishes.", ("topic",)
)


class EventProducer:
    """Lazy producer with bounded retries and visible stream degradation."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        producer: Any | None = None,
        state: DegradationState = degradation_state,
    ) -> None:
        self.settings = settings or get_settings()
        self.state = state
        self._producer = producer
        self._owns_producer = producer is None
        self._started = False
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._producer is None:
                self._producer = AIOKafkaProducer(
                    bootstrap_servers=self.settings.redpanda_bootstrap_servers,
                    client_id=self.settings.redpanda_client_id,
                    acks="all",
                    enable_idempotence=True,
                )
            try:
                await self._producer.start()
                if self._owns_producer:
                    await self._ensure_topics()
            except (KafkaError, OSError, TimeoutError) as exc:
                self.state.mark_down("stream", f"{type(exc).__name__}: {exc}"[:500])
                if self._owns_producer:
                    try:
                        await self._producer.stop()
                    except Exception as stop_exc:  # noqa: BLE001 - producer is discarded
                        logger.debug("failed_producer_stop", reason=str(stop_exc)[:500])
                    self._producer = None
                raise AppError(
                    "stream_unavailable", 503, "Redpanda producer is unavailable"
                ) from exc
            self._started = True
            self.state.mark_healthy("stream")

    async def _ensure_topics(self) -> None:
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self.settings.redpanda_bootstrap_servers,
            client_id=f"{self.settings.redpanda_client_id}-admin",
        )
        await admin.start()
        try:
            existing = await admin.list_topics()
            missing = [
                NewTopic(name, num_partitions=1, replication_factor=1)
                for name in TopicSet.from_settings(self.settings).all()
                if name not in existing
            ]
            if missing:
                await admin.create_topics(missing)
        finally:
            await admin.close()

    async def stop(self) -> None:
        if self._producer is not None and self._started:
            await self._producer.stop()
        self._started = False

    async def send(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str,
        key: str | None = None,
    ) -> EventEnvelope:
        envelope = EventEnvelope(event_type=event_type, payload=payload, trace_id=trace_id)
        return await self.send_envelope(topic, envelope, key=key)

    async def send_envelope(
        self, topic: str, envelope: EventEnvelope, *, key: str | None = None
    ) -> EventEnvelope:
        """Publish a caller-supplied stable envelope, including durable event ID."""

        if not self._started:
            await self.start()
        for attempt in range(3):
            try:
                await self._producer.send_and_wait(
                    topic,
                    envelope.encode(),
                    key=key.encode() if key else None,
                )
            except (KafkaError, OSError, TimeoutError) as exc:
                STREAM_PUBLISH_FAILURES.labels(topic=topic).inc()
                self.state.mark_degraded("stream", f"{type(exc).__name__}: {exc}"[:500])
                if attempt == 2:
                    raise AppError(
                        "stream_publish_failed",
                        503,
                        f"Could not publish {envelope.event_type}",
                    ) from exc
                await asyncio.sleep(0.1 * (2**attempt))
            else:
                self.state.mark_healthy("stream")
                STREAM_PUBLISHED.labels(
                    topic=topic, event_type=envelope.event_type
                ).inc()
                return envelope
        raise AssertionError("unreachable")

    async def health(self) -> bool:
        try:
            if not self._started:
                await self.start()
            await self._producer.client.force_metadata_update()
        except (AppError, KafkaError, OSError, TimeoutError):
            return False
        return True
