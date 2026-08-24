"""Lazy async Redis client lifecycle with an injectable test boundary."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable
from typing import Protocol, cast

from redis.asyncio import from_url as redis_from_url
from redis.exceptions import RedisError

from backend.app.config import Settings, get_settings

REDIS_OPERATION_ERRORS = (RedisError, OSError, TimeoutError)


class RedisLike(Protocol):
    def get(self, key: str) -> Awaitable[object]: ...

    def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[object]: ...

    def delete(self, *keys: object) -> Awaitable[object]: ...

    def ping(self) -> Awaitable[object]: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Awaitable[object]: ...

    def scan_iter(self, *, match: str, count: int = 500) -> AsyncIterator[object]: ...

    def aclose(self) -> Awaitable[object]: ...


class RedisClient:
    """Own a lazy ``redis.asyncio`` client or adapt an injected Redis-like client."""

    def __init__(self, url: str, *, client: RedisLike | None = None) -> None:
        self.url = url
        self._client = client

    @property
    def raw(self) -> RedisLike:
        if self._client is None:
            client = redis_from_url(
                self.url,
                decode_responses=False,
                health_check_interval=30,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self._client = cast(RedisLike, client)
        return self._client

    async def connect(self) -> None:
        """Initialize the connection pool and verify connectivity."""

        await self.raw.ping()

    async def ping(self) -> bool:
        return bool(await self.raw.ping())

    async def health(self) -> bool:
        try:
            return await self.ping()
        except REDIS_OPERATION_ERRORS:
            return False

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._client = None


def create_redis_client(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
) -> RedisClient:
    config = settings or get_settings()
    return RedisClient(config.redis_url, client=client)


__all__ = [
    "REDIS_OPERATION_ERRORS",
    "RedisClient",
    "RedisLike",
    "create_redis_client",
]
