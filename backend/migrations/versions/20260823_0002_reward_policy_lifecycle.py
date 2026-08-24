"""Harden reward idempotency and response-policy lifecycle metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260823_0002"
down_revision: str | None = "20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("policies", sa.Column("active_version", sa.Integer(), nullable=True))
    op.add_column(
        "policy_versions",
        sa.Column("status", sa.String(32), server_default="candidate", nullable=False),
    )
    op.add_column("policy_versions", sa.Column("artifact_uri", sa.Text(), nullable=True))
    op.add_column("policy_versions", sa.Column("artifact_checksum", sa.String(128), nullable=True))
    op.add_column(
        "policy_versions",
        sa.Column("metrics", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "policy_versions",
        sa.Column("gate_result", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("policy_versions", sa.Column("parent_version", sa.Integer(), nullable=True))
    op.add_column("policy_versions", sa.Column("approved_by", sa.String(64), nullable=True))
    op.add_column("policy_versions", sa.Column("activated_at", sa.DateTime(), nullable=True))
    op.create_index("ix_policy_versions_status", "policy_versions", ["status"])
    op.add_column("rewards", sa.Column("idempotency_key", sa.String(160), nullable=True))
    op.add_column(
        "rewards",
        sa.Column("assumptions_version", sa.String(64), server_default="legacy", nullable=False),
    )
    op.add_column(
        "rewards",
        sa.Column("reward_kind", sa.String(32), server_default="observed", nullable=False),
    )
    op.add_column("rewards", sa.Column("evaluation_run_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_rewards_idempotency_key", "rewards", ["idempotency_key"])
    op.create_index("ix_rewards_assumptions_version", "rewards", ["assumptions_version"])
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("message_key", sa.String(160), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("occurred_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("available_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("claim_until", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_outbox_events_status_available",
        "outbox_events",
        ["status", "available_at"],
    )
    op.create_index("ix_outbox_events_claim_until", "outbox_events", ["claim_until"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_claim_until", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_rewards_assumptions_version", table_name="rewards")
    op.drop_constraint("uq_rewards_idempotency_key", "rewards", type_="unique")
    for name in ("evaluation_run_id", "reward_kind", "assumptions_version", "idempotency_key"):
        op.drop_column("rewards", name)
    op.drop_index("ix_policy_versions_status", table_name="policy_versions")
    for name in (
        "activated_at",
        "approved_by",
        "parent_version",
        "gate_result",
        "metrics",
        "artifact_checksum",
        "artifact_uri",
        "status",
    ):
        op.drop_column("policy_versions", name)
    op.drop_column("policies", "active_version")
