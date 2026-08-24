"""Typed Redpanda topic registry and event envelope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import orjson
from pydantic import BaseModel, ConfigDict, Field

from backend.app.config import Settings, get_settings


class EventEnvelope(BaseModel):
    """Versioned event boundary shared by HTTP producers and stream consumers."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    trace_id: str
    payload: dict[str, Any]
    schema_version: int = 1

    def encode(self) -> bytes:
        return orjson.dumps(self.model_dump(mode="json"))

    @classmethod
    def decode(cls, value: bytes | bytearray | memoryview | str) -> EventEnvelope:
        return cls.model_validate(orjson.loads(value))


@dataclass(frozen=True, slots=True)
class TopicSet:
    transactions: str
    fraud_scores: str
    spike_alerts: str
    incidents: str
    agent_events: str
    analyst_actions: str
    responses: str
    outcomes: str
    rewards: str
    alerts: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TopicSet:
        config = settings or get_settings()
        return cls(
            transactions=config.topic_transactions,
            fraud_scores=config.topic_fraud_scores,
            spike_alerts=config.topic_spike_alerts,
            incidents=config.topic_incidents,
            agent_events=config.topic_agent_events,
            analyst_actions=config.topic_analyst_actions,
            responses=config.topic_responses,
            outcomes=config.topic_outcomes,
            rewards=config.topic_rewards,
            alerts=config.topic_alerts,
        )

    def all(self) -> tuple[str, ...]:
        return (
            self.transactions,
            self.fraud_scores,
            self.spike_alerts,
            self.incidents,
            self.agent_events,
            self.analyst_actions,
            self.responses,
            self.outcomes,
            self.rewards,
            self.alerts,
        )
