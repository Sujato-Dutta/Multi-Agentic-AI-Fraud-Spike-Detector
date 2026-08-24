"""Redis-backed cache with resilient process-local degradation."""

from backend.app.cache.cache_service import CacheService, RateLimitResult
from backend.app.cache.keys import (
    ALL_PREFIXES,
    agent_result_key,
    detector_state_key,
    feature_key,
    hash_evidence,
    namespace_prefix,
    prediction_key,
    rate_limit_key,
    session_key,
    transaction_key,
)
from backend.app.cache.redis_client import RedisClient, RedisLike, create_redis_client

__all__ = [
    "ALL_PREFIXES",
    "CacheService",
    "RateLimitResult",
    "RedisClient",
    "RedisLike",
    "agent_result_key",
    "create_redis_client",
    "detector_state_key",
    "feature_key",
    "hash_evidence",
    "namespace_prefix",
    "prediction_key",
    "rate_limit_key",
    "session_key",
    "transaction_key",
]
