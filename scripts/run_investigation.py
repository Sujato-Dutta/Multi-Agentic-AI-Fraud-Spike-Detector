"""Run one persisted incident to the human-review checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.agents.graph import build_investigation_graph
from backend.app.cache.cache_service import CacheService
from backend.app.config import get_settings
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.safety.policy_engine import PolicyEngine
from backend.app.services.investigation_service import InvestigationService


async def run(incident_id: str) -> dict[str, object]:
    settings = get_settings()
    checkpoint_url = settings.checkpoint_database_url or settings.database_url.replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    cache = CacheService(settings=settings)
    await cache.connect()
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as saver:
            await saver.setup()
            gateway = StructuredLLMGateway(cache, settings)
            policy = PolicyEngine.from_yaml(settings.policy_path)
            service = InvestigationService(
                build_investigation_graph(gateway, saver, policy), settings
            )
            return dict(await service.investigate(incident_id))
    finally:
        await cache.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("incident_id")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.incident_id)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
