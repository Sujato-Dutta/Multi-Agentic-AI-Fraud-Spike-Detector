"""Persist and publish deterministic detector incidents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import pandas as pd

from backend.app.config import Settings, get_settings
from backend.app.db.repositories import IncidentRepository
from backend.app.db.session import SessionFactory
from backend.app.ml.spike_detection.detector import SpikeAlert
from backend.app.streaming.topics import TopicSet


class Publisher(Protocol):
    async def send(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str,
        key: str | None = None,
    ) -> object: ...


LocalPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
InvestigationTrigger = Callable[[str, str], Awaitable[None]]


def incident_id_for(alert: SpikeAlert) -> str:
    """Derive an idempotent human-readable incident ID from the alert ID."""

    parts = alert.alert_id.split("-")
    if len(parts) < 3:
        raise ValueError(f"Unsupported alert ID: {alert.alert_id}")
    return f"INC-{parts[-2]}-{parts[-1]}"


class IncidentService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: Any = SessionFactory,
        publisher: Publisher | None = None,
        local_publisher: LocalPublisher | None = None,
        investigation_trigger: InvestigationTrigger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.publisher = publisher
        self.local_publisher = local_publisher
        self.investigation_trigger = investigation_trigger
        self.topics = TopicSet.from_settings(self.settings)

    async def create_from_alert(
        self,
        alert: SpikeAlert,
        segments: Sequence[dict[str, Any]],
        window_rows: pd.DataFrame,
        *,
        trace_id: str,
    ) -> tuple[dict[str, Any], bool]:
        incident_id = incident_id_for(alert)
        exposure = float(
            (
                window_rows["risk_probability"].astype(float)
                * window_rows["amount_inr"].astype(float)
                * self.settings.exposure_loss_factor
            ).sum()
        )
        async with self.session_factory() as session:
            incident, created = await IncidentRepository(session).create(
                incident_id=incident_id,
                alert=alert,
                segments=segments,
                exposure_estimate_inr=exposure,
                trace_id=trace_id,
            )
            await session.commit()
            payload = {
                "incident_id": incident.incident_id,
                "alert_id": incident.alert_id,
                "status": incident.status,
                "detected_at": incident.detected_at.isoformat(),
                "window_start": incident.window_start.isoformat(),
                "window_end": incident.window_end.isoformat(),
                "reason": incident.reason,
                "detector_output": incident.detector_output,
                "exposure_estimate_inr": incident.exposure_estimate_inr,
                "trace_id": incident.trace_id,
                "segments": list(segments),
            }
        if created and self.publisher is not None:
            await self.publisher.send(
                self.topics.spike_alerts,
                "spike.detected",
                alert.to_dict(),
                trace_id=trace_id,
                key=alert.alert_id,
            )
            await self.publisher.send(
                self.topics.incidents,
                "incident.created",
                payload,
                trace_id=trace_id,
                key=incident_id,
            )
        if created and self.local_publisher is not None:
            await self.local_publisher("alert", alert.to_dict())
            await self.local_publisher("incident_update", payload)
        if created and self.investigation_trigger is not None:
            await self.investigation_trigger(incident_id, trace_id)
        return payload, created
