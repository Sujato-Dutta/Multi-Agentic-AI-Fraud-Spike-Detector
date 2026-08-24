from __future__ import annotations

import pytest

from evaluation.dataio import load_features
from tests.conftest import analyst_headers, transaction_payload

pytestmark = pytest.mark.asyncio


async def test_redis_failure_keeps_ingestion_live_and_recovers_visibly(app_stack) -> None:
    app_stack.redis.fail = True
    assert await app_stack.cache.get_json("session:missing") is None
    assert await app_stack.cache.set_session("analyst-1", {"active": True})
    assert await app_stack.cache.get_session("analyst-1") == {"active": True}
    assert await app_stack.cache.claim_transaction("fallback-claim")
    assert not await app_stack.cache.claim_transaction("fallback-claim")

    row = load_features("validation").iloc[0]
    response = await app_stack.client.post(
        "/api/transactions",
        json=transaction_payload(row),
        headers={"X-Service-Token": app_stack.settings.service_token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"]
    analyst = await analyst_headers(app_stack)
    health = await app_stack.client.get("/api/health", headers=analyst)
    assert health.status_code == 200
    assert health.json()["dependencies"]["redis"]["status"] == "degraded"
    stats = app_stack.cache.stats()
    assert stats["failures"] > 0 and stats["fallbacks"] > 0
    assert stats["hits"] > 0 and stats["misses"] > 0

    app_stack.redis.fail = False
    assert await app_stack.cache.ping()
    assert app_stack.state.redis.status == "healthy"
