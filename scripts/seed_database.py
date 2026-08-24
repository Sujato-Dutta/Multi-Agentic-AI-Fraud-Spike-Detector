"""Seed demo users and the active fraud model registry entry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings, get_settings
from backend.app.core.security import hash_password
from backend.app.db.models import User
from backend.app.db.repositories import TransactionRepository
from backend.app.db.session import SessionFactory


async def seed(settings: Settings | None = None, session_factory=SessionFactory) -> dict[str, int]:
    config = settings or get_settings()
    users = (
        (config.demo_analyst_username, config.demo_analyst_password, "analyst"),
        (config.demo_lead_analyst_username, config.demo_lead_analyst_password, "lead_analyst"),
        (config.demo_admin_username, config.demo_admin_password, "admin"),
    )
    created = 0
    async with session_factory() as session:
        for username, password, role in users:
            existing = await session.scalar(select(User).where(User.username == username))
            if existing is None:
                session.add(
                    User(username=username, password_hash=hash_password(password), role=role)
                )
                created += 1
        metadata_path = config.model_dir / "fraud" / "metadata.json"
        artifact_path = config.fraud_primary_model_path
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        await TransactionRepository(session).register_model_version(
            {
                "model_version_id": f"fraud-{hashlib.sha256(artifact_path.read_bytes()).hexdigest()[:12]}",
                "name": "xgboost-fraud-risk",
                "version": "phase2",
                "model_type": "xgboost+isotonic",
                "artifact_uri": str(artifact_path),
                "artifact_version": 1,
                "status": "active",
                "threshold_score_space": metadata["decision_threshold_score_space"],
                "risk_density_score_space": metadata["risk_density_score_space"],
                "metrics": metadata,
            }
        )
        await session.commit()
    return {"users_created": created, "model_versions_registered": 1}


if __name__ == "__main__":
    print(asyncio.run(seed()))
