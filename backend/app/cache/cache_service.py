"""Resilient JSON cache backed by Redis and a bounded process-local LRU."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from typing import Any

import orjson
import structlog
from prometheus_client import Counter, Gauge
from pydantic import BaseModel

from backend.app.cache import keys
from backend.app.cache.redis_client import (
    REDIS_OPERATION_ERRORS,
    RedisClient,
    RedisLike,
    create_redis_client,
)
from backend.app.config import Settings, get_settings
from backend.app.core.runtime import DegradationState, degradation_state
from backend.app.safety.metrics import record_degradation

logger = structlog.get_logger(__name__)

CACHE_HITS_TOTAL = Counter(
    "fraud_cache_hits_total",
    "Successful logical cache reads.",
    ("operation",),
)
CACHE_MISSES_TOTAL = Counter(
    "fraud_cache_misses_total",
    "Logical cache reads with no live value.",
    ("operation",),
)
CACHE_REDIS_FAILURES_TOTAL = Counter(
    "fraud_cache_redis_failures_total",
    "Redis operations that required degraded behavior.",
    ("operation",),
)
CACHE_FALLBACKS_TOTAL = Counter(
    "fraud_cache_fallbacks_total",
    "Cache operations served by the process-local fallback.",
    ("operation",),
)
CACHE_REDIS_AVAILABLE = Gauge(
    "fraud_cache_redis_available",
    "Whether Redis is currently reachable from this process.",
)
CACHE_REDIS_AVAILABLE.set(1)

_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    remaining: int
    reset_after_seconds: int


@dataclass(slots=True)
class _LocalEntry:
    payload: bytes
    expires_at: float


class CacheService:
    """Redis-first cache that degrades to an in-process TTL-aware LRU."""

    def __init__(
        self,
        redis_client: RedisLike | RedisClient | None = None,
        *,
        settings: Settings | None = None,
        state: DegradationState = degradation_state,
        max_entries: int | None = None,
        default_ttl_seconds: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        config = settings or get_settings()
        capacity = max_entries if max_entries is not None else config.cache_max_entries
        ttl = (
            default_ttl_seconds
            if default_ttl_seconds is not None
            else config.cache_default_ttl_seconds
        )
        if capacity < 1 or ttl < 1:
            raise ValueError("Cache capacity and default TTL must be positive")
        self.redis = (
            redis_client
            if isinstance(redis_client, RedisClient)
            else create_redis_client(config, client=redis_client)
        )
        self.state = state
        self.max_entries = capacity
        self.default_ttl_seconds = ttl
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._local: OrderedDict[str, _LocalEntry] = OrderedDict()
        self._local_lock = RLock()
        self._stats_lock = Lock()
        self._stats = {"hits": 0, "misses": 0, "failures": 0, "fallbacks": 0}

    async def connect(self) -> bool:
        try:
            await self.redis.connect()
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("connect", exc, fallback=False)
            return False
        self._redis_success()
        return True

    async def ping(self) -> bool:
        try:
            healthy = await self.redis.ping()
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("ping", exc, fallback=False)
            return False
        if healthy:
            self._redis_success()
            return True
        self._redis_failure("ping", ConnectionError("Redis ping returned a false response"), False)
        return False

    async def health(self) -> bool:
        return await self.ping()

    async def close(self) -> None:
        try:
            await self.redis.close()
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("close", exc, fallback=False)
        with self._local_lock:
            self._local.clear()

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    async def get_json(self, key: str) -> Any | None:
        return await self._get_json(key, "get")

    async def set_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
        nx: bool = False,
    ) -> bool:
        return await self._set_json(key, value, "set", ttl_seconds=ttl_seconds, nx=nx)

    async def claim_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
    ) -> bool | None:
        """Atomically claim a Redis key without process-local fallback.

        ``None`` means Redis could not confirm the claim. Callers using this for
        lifecycle coordination must fail closed rather than treat it as a cache miss.
        """

        ttl = self._ttl(ttl_seconds)
        payload = orjson.dumps(value, default=_json_default)
        try:
            stored = bool(await self.redis.raw.set(key, payload, ex=ttl, nx=True))
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("claim", exc, fallback=False)
            return None
        self._redis_success()
        return stored

    async def get_feature(self, transaction_id: str) -> Any | None:
        return await self._get_json(keys.feature_key(transaction_id), "feature_get")

    async def set_feature(
        self,
        transaction_id: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        return await self._set_json(
            keys.feature_key(transaction_id),
            value,
            "feature_set",
            ttl_seconds=ttl_seconds,
        )

    async def get_prediction(
        self,
        transaction_id: str,
        model_version: str = "active",
    ) -> Any | None:
        return await self._get_json(
            keys.prediction_key(transaction_id, model_version),
            "prediction_get",
        )

    async def set_prediction(
        self,
        transaction_id: str,
        value: object,
        model_version: str = "active",
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        return await self._set_json(
            keys.prediction_key(transaction_id, model_version),
            value,
            "prediction_set",
            ttl_seconds=ttl_seconds,
        )

    async def get_detector_state(self, detector_id: str = "default") -> Any | None:
        return await self._get_json(keys.detector_state_key(detector_id), "detector_get")

    async def set_detector_state(
        self,
        value: object,
        detector_id: str = "default",
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        return await self._set_json(
            keys.detector_state_key(detector_id),
            value,
            "detector_set",
            ttl_seconds=ttl_seconds,
        )

    async def get_agent_result(
        self, tier: str, prompt_hash: str, evidence_hash: str
    ) -> Any | None:
        return await self._get_json(
            keys.agent_result_key(tier, prompt_hash, evidence_hash), "agent_result_get"
        )

    async def set_agent_result(
        self,
        tier: str,
        prompt_hash: str,
        evidence_hash: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        return await self._set_json(
            keys.agent_result_key(tier, prompt_hash, evidence_hash),
            value,
            "agent_result_set",
            ttl_seconds=ttl_seconds,
        )

    async def get_session(self, session_id: str) -> Any | None:
        return await self._get_json(keys.session_key(session_id), "session_get")

    async def set_session(
        self,
        session_id: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        return await self._set_json(
            keys.session_key(session_id),
            value,
            "session_set",
            ttl_seconds=ttl_seconds,
        )

    async def claim_transaction(
        self,
        transaction_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Claim once via Redis SET NX and a process-local atomic NX guard.

        The local claim prevents fail-open duplicates within this process; PostgreSQL remains
        authoritative across worker crashes and processes.
        """

        return await self._set_json(
            keys.transaction_key(transaction_id),
            {"claimed": True},
            "transaction_claim",
            ttl_seconds=ttl_seconds,
            nx=True,
        )

    async def release_transaction(self, transaction_id: str) -> bool:
        return bool(await self.delete(keys.transaction_key(transaction_id)))

    async def check_rate_limit(
        self,
        scope: str,
        identity: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit limit and window must be positive")
        now = self._wall_time()
        bucket = int(now // window_seconds)
        reset_after = max(1, int(((bucket + 1) * window_seconds) - now + 0.999999))
        key = keys.rate_limit_key(scope, identity, bucket)
        local_count = self._local_increment(key, reset_after)
        try:
            result = await self.redis.raw.eval(
                _RATE_LIMIT_SCRIPT,
                1,
                key,
                reset_after,
            )
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("rate_limit", exc)
            count = local_count
        else:
            self._redis_success()
            count = self._rate_limit_count(result)
        return RateLimitResult(
            allowed=count <= limit,
            count=count,
            limit=limit,
            remaining=max(0, limit - count),
            reset_after_seconds=reset_after,
        )

    async def delete(self, key: str) -> int:
        return await self.delete_many(key)

    async def delete_many(self, *cache_keys: str) -> int:
        if not cache_keys:
            return 0
        local_deleted = self._local_delete(*cache_keys)
        try:
            deleted = int(await self.redis.raw.delete(*cache_keys))
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("delete", exc)
            return local_deleted
        self._redis_success()
        return max(deleted, local_deleted)

    async def clear_prefix(self, prefix: str) -> int:
        """Delete one known cache namespace without allowing broad Redis wipes."""

        if prefix not in keys.ALL_PREFIXES:
            raise ValueError("Only a complete known cache namespace may be cleared")
        local_deleted = self._local_clear_prefix(prefix)
        deleted = 0
        batch: list[object] = []
        try:
            async for key in self.redis.raw.scan_iter(match=f"{prefix}*", count=500):
                batch.append(key)
                if len(batch) == 500:
                    deleted += int(await self.redis.raw.delete(*batch))
                    batch.clear()
            if batch:
                deleted += int(await self.redis.raw.delete(*batch))
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure("clear_prefix", exc)
            return local_deleted
        self._redis_success()
        return max(deleted, local_deleted)

    async def _get_json(self, key: str, operation: str) -> Any | None:
        try:
            raw = await self.redis.raw.get(key)
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure(operation, exc)
            found, payload = self._local_get(key)
        else:
            self._redis_success()
            found = raw is not None
            payload = self._payload_bytes(raw) if found else None
        self._record_read(operation, found)
        return orjson.loads(payload) if payload is not None else None

    async def _set_json(
        self,
        key: str,
        value: object,
        operation: str,
        *,
        ttl_seconds: int | None,
        nx: bool = False,
    ) -> bool:
        ttl = self._ttl(ttl_seconds)
        payload = orjson.dumps(value, default=_json_default)
        if nx and not self._local_set(key, payload, ttl, nx=True):
            return False
        try:
            stored = bool(await self.redis.raw.set(key, payload, ex=ttl, nx=nx))
        except REDIS_OPERATION_ERRORS as exc:
            self._redis_failure(operation, exc)
            if nx:
                return True
            return self._local_set(key, payload, ttl)
        self._redis_success()
        if stored and not nx:
            self._local_set(key, payload, ttl)
        return stored

    def _local_get(self, key: str) -> tuple[bool, bytes | None]:
        now = self._monotonic()
        with self._local_lock:
            entry = self._local.get(key)
            if entry is None:
                return False, None
            if entry.expires_at <= now:
                del self._local[key]
                return False, None
            self._local.move_to_end(key)
            return True, entry.payload

    def _local_set(self, key: str, payload: bytes, ttl_seconds: int, *, nx: bool = False) -> bool:
        now = self._monotonic()
        with self._local_lock:
            existing = self._local.get(key)
            if existing is not None and existing.expires_at <= now:
                del self._local[key]
                existing = None
            if nx and existing is not None:
                return False
            self._purge_expired_unlocked(now)
            self._local[key] = _LocalEntry(payload, now + ttl_seconds)
            self._local.move_to_end(key)
            while len(self._local) > self.max_entries:
                self._local.popitem(last=False)
            return True

    def _local_increment(self, key: str, ttl_seconds: int) -> int:
        now = self._monotonic()
        with self._local_lock:
            entry = self._local.get(key)
            count = 0
            if entry is not None and entry.expires_at > now:
                count = int(orjson.loads(entry.payload))
            count += 1
            self._local[key] = _LocalEntry(orjson.dumps(count), now + ttl_seconds)
            self._local.move_to_end(key)
            while len(self._local) > self.max_entries:
                self._local.popitem(last=False)
            return count

    def _local_delete(self, *cache_keys: str) -> int:
        with self._local_lock:
            return sum(self._local.pop(key, None) is not None for key in cache_keys)

    def _local_clear_prefix(self, prefix: str) -> int:
        with self._local_lock:
            matches = [key for key in self._local if key.startswith(prefix)]
            for key in matches:
                del self._local[key]
            return len(matches)

    def _purge_expired_unlocked(self, now: float) -> None:
        expired = [key for key, entry in self._local.items() if entry.expires_at <= now]
        for key in expired:
            del self._local[key]

    def _record_read(self, operation: str, hit: bool) -> None:
        name = "hits" if hit else "misses"
        with self._stats_lock:
            self._stats[name] += 1
        metric = CACHE_HITS_TOTAL if hit else CACHE_MISSES_TOTAL
        metric.labels(operation=operation).inc()

    def _redis_failure(
        self,
        operation: str,
        exc: BaseException,
        fallback: bool = True,
    ) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        self.state.mark_degraded("redis", reason)
        record_degradation("redis_down")
        CACHE_REDIS_AVAILABLE.set(0)
        CACHE_REDIS_FAILURES_TOTAL.labels(operation=operation).inc()
        with self._stats_lock:
            self._stats["failures"] += 1
            if fallback:
                self._stats["fallbacks"] += 1
        if fallback:
            CACHE_FALLBACKS_TOTAL.labels(operation=operation).inc()
        logger.warning("redis_degraded", operation=operation, reason=reason, fallback=fallback)

    def _redis_success(self) -> None:
        self.state.mark_healthy("redis")
        CACHE_REDIS_AVAILABLE.set(1)

    def _ttl(self, value: int | None) -> int:
        ttl = self.default_ttl_seconds if value is None else value
        if ttl < 1:
            raise ValueError("Cache TTL must be positive")
        return ttl

    @staticmethod
    def _payload_bytes(value: object) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray | memoryview):
            return bytes(value)
        if isinstance(value, str):
            return value.encode()
        raise TypeError(f"Redis returned unsupported payload type: {type(value).__name__}")

    @staticmethod
    def _rate_limit_count(value: object) -> int:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if not value:
                raise ValueError("Redis returned an empty rate-limit result")
            return int(value[0])
        return int(value)


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Value {type(value).__name__} is not JSON serializable")
