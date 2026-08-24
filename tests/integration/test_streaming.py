from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backend.app.db.models import FraudScore, Transaction
from backend.app.streaming.consumer import EventConsumer
from backend.app.streaming.topics import EventEnvelope, TopicSet
from evaluation.dataio import load_features
from tests.conftest import transaction_payload

pytestmark = pytest.mark.asyncio


class FakeConsumer:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    def highwater(self, topic_partition) -> int:
        return 1


async def test_manual_commit_duplicate_idempotency_and_validation_equivalence(app_stack) -> None:
    row = load_features("validation").iloc[0]
    payload = transaction_payload(row)
    topics = TopicSet.from_settings(app_stack.settings)
    fake = FakeConsumer()
    consumer = EventConsumer(
        {topics.transactions: app_stack.service.handle_stream_event},
        app_stack.settings,
        consumer=fake,
        state=app_stack.state,
    )
    envelope = EventEnvelope(
        event_type="transaction.received", trace_id="stream-test", payload=payload
    )
    message = SimpleNamespace(
        topic=topics.transactions,
        value=envelope.encode(),
        offset=0,
        partition=0,
        topic_partition=(topics.transactions, 0),
    )
    first = await consumer.handle_message(message)
    second = await consumer.handle_message(message)
    assert first.created and not second.created
    assert fake.commits == 2
    async with app_stack.session_factory() as session:
        transaction_count = await session.scalar(select(func.count()).select_from(Transaction))
        score_count = await session.scalar(select(func.count()).select_from(FraudScore))
    assert transaction_count == score_count == 1

    validation = load_features("validation")
    online_alerts = await app_stack.service.verify_replay_equivalence(validation)
    report = json.loads(
        app_stack.settings.report_dir.joinpath("metrics/phase2_benchmark.json").read_text(
            encoding="utf-8"
        )
    )
    expected = report["validation"]["spikes"]["alerts"]
    assert [item["alert_id"] for item in online_alerts] == [item["alert_id"] for item in expected]
    assert [item["fire_timestamp"] for item in online_alerts] == [
        item["fire_timestamp"] for item in expected
    ]
    assert all(
        abs(
            __import__("pandas").Timestamp(actual["fire_timestamp"])
            - __import__("pandas").Timestamp(reference["fire_timestamp"])
        ).total_seconds()
        <= app_stack.settings.detector_slide_minutes * 60
        for actual, reference in zip(online_alerts, expected, strict=True)
    )
