"""Explicitly reset demo persistence/cache and reseed identities."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.cache import keys
from backend.app.cache.cache_service import CacheService
from backend.app.db.session import SessionFactory
from scripts.seed_database import seed


async def reset() -> dict[str, int]:
    async with SessionFactory() as session:
        await session.execute(
            text(
                "TRUNCATE audit_events, incident_memory, rewards, policy_versions, policies, "
                "analyst_decisions, agent_outputs, evidence, incident_segments, incidents, "
                "fraud_scores, transactions, model_versions, users RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    cache = CacheService()
    cleared = 0
    for prefix in keys.ALL_PREFIXES:
        cleared += await cache.clear_prefix(prefix)
    await cache.close()
    result = await seed()
    result["cache_entries_cleared"] = cleared
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Confirm destructive demo reset")
    args = parser.parse_args()
    if not args.yes:
        parser.error("Pass --yes to confirm deletion of all demo data")
    print(asyncio.run(reset()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
