"""Small typed WebSocket broadcast hub."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

from backend.app.schemas import WebSocketMessage


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, message_type: str, payload: dict[str, Any]) -> None:
        message = WebSocketMessage(
            type=message_type,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            payload=payload,
        ).model_dump(mode="json")
        async with self._lock:
            clients = tuple(self._clients)
        failed: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_json(message)
            except Exception:  # noqa: BLE001 - disconnected sockets are removed
                failed.append(client)
        if failed:
            async with self._lock:
                self._clients.difference_update(failed)
