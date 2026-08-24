"""Process-wide dependency health, application errors, and replay time."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal, cast

DependencyName = Literal[
    "postgres",
    "redis",
    "stream",
    "fraud_model",
    "llm",
    "checkpoint",
    "reward_model",
    "response_policy",
]
DependencyStatus = Literal["healthy", "degraded", "down"]
_DEPENDENCIES: tuple[DependencyName, ...] = (
    "postgres",
    "redis",
    "stream",
    "fraud_model",
    "llm",
    "checkpoint",
    "reward_model",
    "response_policy",
)
_STATUSES = frozenset({"healthy", "degraded", "down"})


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    """Immutable health record returned by ``DegradationState``."""

    status: DependencyStatus
    reason: str | None
    changed_at: datetime


class DegradationState:
    """Thread-safe process-wide dependency health registry."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = RLock()
        self._states: dict[DependencyName, DependencyHealth] = {}
        self.reset()

    def set(
        self,
        dependency: DependencyName | str,
        status: DependencyStatus | str,
        reason: str | None = None,
    ) -> DependencyHealth:
        name = self._dependency_name(dependency)
        if status not in _STATUSES:
            raise ValueError(f"Unsupported dependency status: {status!r}")
        typed_status = cast(DependencyStatus, status)
        normalized_reason = reason.strip() if reason and reason.strip() else None
        if typed_status == "healthy":
            normalized_reason = None
        with self._lock:
            current = self._states.get(name)
            if (
                current is not None
                and current.status == typed_status
                and current.reason == normalized_reason
            ):
                return current
            health = DependencyHealth(
                status=typed_status,
                reason=normalized_reason,
                changed_at=_utc_naive(self._clock()),
            )
            self._states[name] = health
            return health

    def get(self, dependency: DependencyName | str) -> DependencyHealth:
        name = self._dependency_name(dependency)
        with self._lock:
            return self._states[name]

    def mark_healthy(self, dependency: DependencyName | str) -> DependencyHealth:
        return self.set(dependency, "healthy")

    def mark_degraded(self, dependency: DependencyName | str, reason: str) -> DependencyHealth:
        return self.set(dependency, "degraded", reason)

    def mark_down(self, dependency: DependencyName | str, reason: str) -> DependencyHealth:
        return self.set(dependency, "down", reason)

    def snapshot(self) -> dict[str, dict[str, str | datetime | None]]:
        """Return a detached, JSON-friendly snapshot of every dependency."""

        with self._lock:
            return {
                name: {
                    "status": health.status,
                    "reason": health.reason,
                    "changed_at": health.changed_at,
                }
                for name, health in self._states.items()
            }

    def reset(self) -> None:
        """Reset every dependency to healthy, primarily for process startup and tests."""

        changed_at = _utc_naive(self._clock())
        with self._lock:
            self._states = {
                name: DependencyHealth("healthy", None, changed_at) for name in _DEPENDENCIES
            }

    @property
    def postgres(self) -> DependencyHealth:
        return self.get("postgres")

    @property
    def redis(self) -> DependencyHealth:
        return self.get("redis")

    @property
    def stream(self) -> DependencyHealth:
        return self.get("stream")

    @property
    def fraud_model(self) -> DependencyHealth:
        return self.get("fraud_model")

    @property
    def llm(self) -> DependencyHealth:
        return self.get("llm")

    @staticmethod
    def _dependency_name(value: DependencyName | str) -> DependencyName:
        if value not in _DEPENDENCIES:
            raise ValueError(f"Unsupported dependency: {value!r}")
        return cast(DependencyName, value)


class AppError(Exception):
    """Compact error rendered uniformly by the future API exception handler."""

    def __init__(self, code: str, http_status: int, detail: str) -> None:
        if not code or not detail:
            raise ValueError("AppError code and detail must be non-empty")
        if not 400 <= http_status <= 599:
            raise ValueError("AppError http_status must be between 400 and 599")
        self.code = code
        self.http_status = http_status
        self.detail = detail
        super().__init__(detail)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


class VirtualClock:
    """UTC-naive replay clock where ``speed`` event seconds pass per real second."""

    def __init__(
        self,
        speed: float = 1.0,
        *,
        start: datetime | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._validate_speed(speed)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._lock = RLock()
        self._advance_lock = asyncio.Lock()
        self._speed = float(speed)
        self._anchor_real = monotonic()
        self._anchor_virtual = _utc_naive(start or wall_clock())

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        self.set_speed(value)

    def set_speed(self, speed: float) -> None:
        """Change replay speed without discontinuity in virtual time."""

        self._validate_speed(speed)
        with self._lock:
            current = self._now_unlocked()
            self._anchor_virtual = current
            self._anchor_real = self._monotonic()
            self._speed = float(speed)

    def real_now(self) -> datetime:
        """Return real UTC time independently of replay speed."""

        return _utc_naive(self._wall_clock())

    def now(self) -> datetime:
        """Return current virtual UTC time."""

        with self._lock:
            return self._now_unlocked()

    def set(self, value: datetime) -> None:
        """Set virtual time immediately."""

        with self._lock:
            self._anchor_virtual = _utc_naive(value)
            self._anchor_real = self._monotonic()

    async def wait(self, delay: timedelta | float) -> None:
        """Wait for a virtual duration, scaled down by replay speed."""

        seconds = self._seconds(delay)
        await self._sleeper(seconds / self.speed)

    async def wait_until(self, target: datetime) -> None:
        """Wait until virtual time reaches ``target`` without moving time backwards."""

        delay = (_utc_naive(target) - self.now()).total_seconds()
        if delay > 0:
            await self.wait(delay)

    async def advance(self, delay: timedelta | float) -> datetime:
        """Wait for and then pin an exact virtual-time advance for deterministic replay."""

        seconds = self._seconds(delay)
        async with self._advance_lock:
            target = self.now() + timedelta(seconds=seconds)
            await self._sleeper(seconds / self.speed)
            self.set(target)
            return target

    async def advance_to(self, target: datetime) -> datetime:
        """Advance to an exact event timestamp, rejecting out-of-order replay."""

        normalized = _utc_naive(target)
        async with self._advance_lock:
            delay = (normalized - self.now()).total_seconds()
            if delay < 0:
                raise ValueError("VirtualClock cannot advance backwards")
            await self._sleeper(delay / self.speed)
            self.set(normalized)
            return normalized

    def _now_unlocked(self) -> datetime:
        elapsed = max(0.0, self._monotonic() - self._anchor_real)
        return self._anchor_virtual + timedelta(seconds=elapsed * self._speed)

    @staticmethod
    def _seconds(value: timedelta | float) -> float:
        seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Clock delay must be a finite non-negative duration")
        return seconds

    @staticmethod
    def _validate_speed(value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("VirtualClock speed must be finite and positive")


degradation_state = DegradationState()
