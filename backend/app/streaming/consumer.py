"""Manual-commit ordered Redpanda consumer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.errors import KafkaError
from prometheus_client import Counter, Gauge

from backend.app.config import Settings, get_settings
from backend.app.core.runtime import DegradationState, degradation_state
from backend.app.safety.metrics import record_degradation
from backend.app.streaming.topics import EventEnvelope

logger = structlog.get_logger(__name__)
EventHandler = Callable[[dict[str, Any], str], Awaitable[object]]
STREAM_CONSUMED = Counter(
    "fraud_stream_consumed_total", "Successfully handled Redpanda events.", ("topic", "event_type")
)
STREAM_HANDLER_FAILURES = Counter(
    "fraud_stream_handler_failures_total", "Failed stream handler calls.", ("topic",)
)
STREAM_CONSUMER_LAG = Gauge(
    "fraud_stream_consumer_lag", "Approximate consumer lag.", ("topic", "partition")
)


class EventConsumer:
    """One ordered consumer; commits each offset only after its handler succeeds."""

    def __init__(
        self,
        handlers: Mapping[str, EventHandler],
        settings: Settings | None = None,
        *,
        consumer: Any | None = None,
        state: DegradationState = degradation_state,
    ) -> None:
        if not handlers:
            raise ValueError("At least one stream handler is required")
        self.settings = settings or get_settings()
        self.handlers = dict(handlers)
        self.state = state
        self._consumer = consumer
        self._started = False
        self._stop = asyncio.Event()
        self._lagging_partitions: set[str] = set()
        self._handler_failures: dict[tuple[str, int, int], int] = {}
        self._handler_logs: dict[tuple[str, int, int], tuple[str, float]] = {}

    async def start(self) -> None:
        if self._started:
            return
        if self._consumer is None:
            self._consumer = AIOKafkaConsumer(
                *self.handlers,
                bootstrap_servers=self.settings.redpanda_bootstrap_servers,
                client_id=f"{self.settings.redpanda_client_id}-consumer",
                group_id=self.settings.redpanda_consumer_group,
                enable_auto_commit=False,
                auto_offset_reset=self.settings.redpanda_auto_offset_reset,
                max_poll_records=self.settings.stream_max_poll_records,
            )
        try:
            await self._consumer.start()
        except (KafkaError, OSError, TimeoutError) as exc:
            self.state.mark_down("stream", f"{type(exc).__name__}: {exc}"[:500])
            raise
        self._started = True
        self._stop.clear()
        self.state.mark_healthy("stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._consumer is not None and self._started:
            await self._consumer.stop()
        self._started = False

    async def run(self) -> None:
        if not self._started:
            await self.start()
        try:
            async for message in self._consumer:
                if self._stop.is_set():
                    break
                await self._handle_with_retry(message)
        except asyncio.CancelledError:
            raise
        except (KafkaError, OSError, TimeoutError) as exc:
            self.state.mark_down("stream", f"{type(exc).__name__}: {exc}"[:500])
            logger.error("stream_consumer_stopped", reason=str(exc))
            raise
        except Exception as exc:
            self.state.mark_down("stream", f"{type(exc).__name__}: {exc}"[:500])
            logger.exception("stream_consumer_stopped", reason=str(exc)[:500])
            raise

    async def _handle_with_retry(self, message: Any) -> object | None:
        key = (message.topic, message.partition, message.offset)
        try:
            result = await self.handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - seek keeps the failed offset retryable
            failures = self._handler_failures.get(key, 0) + 1
            self._handler_failures[key] = failures
            reason = f"{type(exc).__name__}: {exc}"[:500]
            self.state.mark_degraded(
                "stream", f"handler:{message.topic}:{reason}"[:500]
            )
            delay = min(
                self.settings.stream_handler_retry_max_seconds,
                0.5 * (2 ** min(failures - 1, 20)),
            )
            last_reason, last_logged_at = self._handler_logs.get(key, ("", 0.0))
            now = time.monotonic()
            if (
                reason != last_reason
                or now - last_logged_at
                >= self.settings.stream_handler_log_interval_seconds
            ):
                logger.warning(
                    "stream_handler_retry",
                    topic=message.topic,
                    partition=message.partition,
                    offset=message.offset,
                    reason=reason,
                    consecutive_failures=failures,
                    retry_seconds=delay,
                )
                self._handler_logs[key] = (reason, now)
            topic_partition = TopicPartition(message.topic, message.partition)
            self._consumer.seek(topic_partition, message.offset)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            return None
        failures = self._handler_failures.pop(key, 0)
        self._handler_logs.pop(key, None)
        if failures:
            logger.info(
                "stream_handler_recovered",
                topic=message.topic,
                partition=message.partition,
                offset=message.offset,
                failed_attempts=failures,
            )
        return result

    async def handle_message(self, message: Any) -> object:
        envelope = EventEnvelope.decode(message.value)
        handler = self.handlers.get(message.topic)
        if handler is None:
            raise ValueError(f"No handler registered for topic {message.topic}")
        try:
            result = await handler(envelope.payload, envelope.trace_id)
        except Exception:
            STREAM_HANDLER_FAILURES.labels(topic=message.topic).inc()
            raise
        await self._consumer.commit()
        STREAM_CONSUMED.labels(topic=message.topic, event_type=envelope.event_type).inc()
        topic_partition = TopicPartition(message.topic, message.partition)
        highwater = self._consumer.highwater(topic_partition)
        if highwater is not None:
            lag = max(0, highwater - message.offset - 1)
            partition_key = f"{message.topic}:{message.partition}"
            STREAM_CONSUMER_LAG.labels(
                topic=message.topic, partition=str(message.partition)
            ).set(lag)
            if lag >= self.settings.stream_lag_alert_threshold:
                if partition_key not in self._lagging_partitions:
                    record_degradation("stream_lag")
                    logger.warning(
                        "stream_lag_degraded",
                        topic=message.topic,
                        partition=message.partition,
                        lag=lag,
                    )
                self._lagging_partitions.add(partition_key)
                self.state.mark_degraded("stream", f"consumer_lag:{partition_key}:{lag}")
            else:
                self._lagging_partitions.discard(partition_key)
        if not self._lagging_partitions:
            self.state.mark_healthy("stream")
        return result

    async def health(self) -> bool:
        return self._started and not self._stop.is_set()
