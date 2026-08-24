from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.cache.cache_service import CacheService
from backend.app.config import Settings
from backend.app.core.runtime import DegradationState
from backend.app.core.security import hash_password
from backend.app.db.models import Base, User
from backend.app.main import create_app
from backend.app.services.incident_service import IncidentService
from backend.app.services.transaction_service import TransactionService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail = False
        self.rate_counts: dict[str, int] = {}

    def _check(self) -> None:
        if self.fail:
            raise OSError("redis unavailable")

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> bytes | None:
        self._check()
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex=None, nx: bool = False) -> bool:
        self._check()
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, *keys: object) -> int:
        self._check()
        deleted = 0
        for key in keys:
            deleted += self.values.pop(str(key), None) is not None
        return deleted

    async def eval(self, script: str, numkeys: int, *args: object) -> list[int]:
        self._check()
        key = str(args[0])
        self.rate_counts[key] = self.rate_counts.get(key, 0) + 1
        return [self.rate_counts[key], int(args[1])]

    async def scan_iter(self, *, match: str, count: int = 500):
        self._check()
        prefix = match.removesuffix("*")
        for key in tuple(self.values):
            if key.startswith(prefix):
                yield key

    async def aclose(self) -> None:
        return None


@dataclass
class AppStack:
    client: httpx.AsyncClient
    settings: Settings
    session_factory: Any
    service: TransactionService
    cache: CacheService
    redis: FakeRedis
    state: DegradationState
    app: Any


@pytest_asyncio.fixture
async def app_stack(tmp_path: Path):
    database_path = tmp_path / "phase3.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        stream_consumer_enabled=False,
        investigation_auto_start=False,
        jwt_secret_key="phase3-test-jwt-secret-at-least-32-bytes-long",
        service_token="phase3-test-service-token",
        demo_analyst_password="phase3-analyst-password",
        demo_lead_analyst_password="phase3-lead-password",
        demo_admin_password="phase3-admin-password",
    )
    async with factory() as session:
        session.add(
            User(
                username=settings.demo_analyst_username,
                password_hash=hash_password(settings.demo_analyst_password),
                role="analyst",
            )
        )
        await session.commit()
    fake_redis = FakeRedis()
    state = DegradationState()
    cache = CacheService(fake_redis, settings=settings, state=state)
    incident_service = IncidentService(settings, session_factory=factory)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = TransactionService(
            settings,
            cache=cache,
            session_factory=factory,
            incident_service=incident_service,
            state=state,
        )
    app = create_app(
        settings,
        session_factory=factory,
        cache=cache,
        transaction_service=service,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield AppStack(
                client, settings, factory, service, cache, fake_redis, state, app
            )
    await engine.dispose()


async def analyst_headers(stack: AppStack) -> dict[str, str]:
    response = await stack.client.post(
        "/api/auth/token",
        json={
            "username": stack.settings.demo_analyst_username,
            "password": stack.settings.demo_analyst_password,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def transaction_payload(row: Any) -> dict[str, Any]:
    return orjson.loads(orjson.dumps(row.to_dict(), default=lambda value: value.isoformat() if hasattr(value, "isoformat") else value.item()))
