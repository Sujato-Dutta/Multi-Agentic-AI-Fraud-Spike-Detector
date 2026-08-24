from __future__ import annotations

import pytest

from evaluation.dataio import load_features
from tests.conftest import analyst_headers, transaction_payload

pytestmark = pytest.mark.asyncio


async def test_auth_health_ingest_and_incident_detail(app_stack) -> None:
    assert (await app_stack.client.get("/api/health")).status_code == 401
    analyst = await analyst_headers(app_stack)
    health = await app_stack.client.get("/api/health", headers=analyst)
    assert health.status_code == 200
    assert health.json()["dependencies"]["stream"]["status"] == "degraded"

    validation = load_features("validation")
    unauthorized = await app_stack.client.post(
        "/api/transactions/batch",
        json={"transactions": [transaction_payload(validation.iloc[0])]},
    )
    assert unauthorized.status_code == 401
    accepted = 0
    for start in range(0, len(validation), app_stack.settings.max_ingest_batch_size):
        rows = validation.iloc[start : start + app_stack.settings.max_ingest_batch_size]
        response = await app_stack.client.post(
            "/api/transactions/batch",
            json={"transactions": [transaction_payload(row) for _, row in rows.iterrows()]},
            headers={"X-Service-Token": app_stack.settings.service_token},
        )
        assert response.status_code == 200, response.text
        accepted += response.json()["accepted"]
    assert accepted == len(validation)

    incidents = await app_stack.client.get("/api/incidents", headers=analyst)
    assert incidents.status_code == 200
    assert incidents.json()["count"] == 2
    incident_id = incidents.json()["items"][0]["incident_id"]
    detail = await app_stack.client.get(f"/api/incidents/{incident_id}", headers=analyst)
    body = detail.json()
    assert detail.status_code == 200
    assert body["status"] == "detected"
    assert body["alert_id"].startswith("SPIKE-")
    assert body["exposure_estimate_inr"] > 0
    assert "detector_output" in body and "segments" in body and "timeline" in body
