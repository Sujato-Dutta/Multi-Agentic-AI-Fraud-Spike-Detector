"""Create the initial fraud detector schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260822_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=False), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("ip_cluster_id", sa.String(length=64), nullable=False),
        sa.Column("amount_inr", sa.Float(), nullable=False),
        sa.Column("merchant_category", sa.String(length=64), nullable=False),
        sa.Column("payment_method", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("customer_age_days", sa.Integer(), nullable=False),
        sa.Column("customer_txn_count_30d", sa.Integer(), nullable=False),
        sa.Column("customer_avg_amount_30d", sa.Float(), nullable=False),
        sa.Column("time_since_last_txn_min", sa.Float(), nullable=False),
        sa.Column("txn_velocity_10m", sa.Integer(), nullable=False),
        sa.Column("txn_velocity_1h", sa.Integer(), nullable=False),
        sa.Column("amount_zscore_customer", sa.Float(), nullable=False),
        sa.Column("is_new_device", sa.Boolean(), nullable=False),
        sa.Column("device_trust_score", sa.Float(), nullable=False),
        sa.Column("ip_risk_score", sa.Float(), nullable=False),
        sa.Column("geo_distance_km", sa.Float(), nullable=False),
        sa.Column("billing_shipping_mismatch", sa.Boolean(), nullable=False),
        sa.Column("failed_attempts_24h", sa.Integer(), nullable=False),
        sa.Column("account_changes_24h", sa.Integer(), nullable=False),
        sa.Column("is_proxy_ip", sa.Boolean(), nullable=False),
        sa.Column("prior_disputes_90d", sa.Integer(), nullable=False),
        sa.Column("merchant_fraud_rate_7d", sa.Float(), nullable=False),
        sa.Column("known_promo_event", sa.Boolean(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("is_weekend", sa.Boolean(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("transaction_id", name="pk_transactions"),
    )
    op.create_index("ix_transactions_timestamp", "transactions", ["timestamp"])

    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "model_versions",
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("model_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="registered", nullable=False),
        sa.Column("threshold_score_space", sa.String(length=64), nullable=True),
        sa.Column("risk_density_score_space", sa.String(length=64), nullable=True),
        sa.Column("metrics", JSON_DOCUMENT, nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("model_version_id", name="pk_model_versions"),
        sa.UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
    )
    op.create_index("ix_model_versions_status", "model_versions", ["status"])
    op.create_index("ix_model_versions_registered_at", "model_versions", ["registered_at"])

    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("alert_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="detected", nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=False), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=False), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detector_output", JSON_DOCUMENT, nullable=False),
        sa.Column("exposure_estimate_inr", sa.Float(), server_default="0", nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("incident_id", name="pk_incidents"),
        sa.UniqueConstraint("alert_id", name="uq_incidents_alert_id"),
    )
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_detected_at", "incidents", ["detected_at"])

    op.create_table(
        "fraud_scores",
        sa.Column("score_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=True),
        sa.Column("risk_probability", sa.Float(), nullable=False),
        sa.Column("decision_score", sa.Float(), nullable=False),
        sa.Column("decision_threshold", sa.Float(), nullable=False),
        sa.Column("score_space", sa.String(length=64), nullable=False),
        sa.Column("degraded", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "scored_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_fraud_scores_model_version_id_model_versions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transaction_id"],
            name="fk_fraud_scores_transaction_id_transactions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("score_id", name="pk_fraud_scores"),
        sa.UniqueConstraint("transaction_id", name="uq_fraud_scores_transaction_id"),
    )
    op.create_index("ix_fraud_scores_scored_at", "fraud_scores", ["scored_at"])
    op.create_index("ix_fraud_scores_model_version_id", "fraud_scores", ["model_version_id"])

    op.create_table(
        "incident_segments",
        sa.Column("segment_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("conditions", JSON_DOCUMENT, nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("baseline_support", sa.Integer(), nullable=False),
        sa.Column("risk_density", sa.Float(), nullable=False),
        sa.Column("baseline_risk_density", sa.Float(), nullable=False),
        sa.Column("density_lift", sa.Float(), nullable=False),
        sa.Column("prevalence_lift", sa.Float(), nullable=False),
        sa.Column("excess_risk_contribution", sa.Float(), nullable=False),
        sa.Column("p_value", sa.Float(), nullable=False),
        sa.Column("rank_score", sa.Float(), nullable=False),
        sa.Column("condition_contributions", JSON_DOCUMENT, nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_incident_segments_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("segment_id", name="pk_incident_segments"),
        sa.UniqueConstraint(
            "incident_id",
            "rank",
            name="uq_incident_segments_incident_rank",
        ),
    )
    op.create_index("ix_incident_segments_incident_id", "incident_segments", ["incident_id"])

    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("strength", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_evidence_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_evidence"),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"])
    op.create_index("ix_evidence_created_at", "evidence", ["created_at"])

    op.create_table(
        "agent_outputs",
        sa.Column("output_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("evidence_hash", sa.String(length=128), nullable=True),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_agent_outputs_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("output_id", name="pk_agent_outputs"),
    )
    op.create_index("ix_agent_outputs_incident_id", "agent_outputs", ["incident_id"])
    op.create_index("ix_agent_outputs_status", "agent_outputs", ["status"])
    op.create_index("ix_agent_outputs_created_at", "agent_outputs", ["created_at"])

    op.create_table(
        "analyst_decisions",
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("actor_username", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="recorded", nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("original_recommendation", JSON_DOCUMENT, nullable=False),
        sa.Column("final_action", JSON_DOCUMENT, nullable=False),
        sa.Column("outcome", JSON_DOCUMENT, nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("outcome_recorded_at", sa.DateTime(timezone=False), nullable=True),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_analyst_decisions_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("decision_id", name="pk_analyst_decisions"),
    )
    op.create_index("ix_analyst_decisions_incident_id", "analyst_decisions", ["incident_id"])
    op.create_index("ix_analyst_decisions_status", "analyst_decisions", ["status"])
    op.create_index("ix_analyst_decisions_decided_at", "analyst_decisions", ["decided_at"])

    op.create_table(
        "policies",
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_id", name="pk_policies"),
        sa.UniqueConstraint("name", name="uq_policies_name"),
    )
    op.create_index("ix_policies_status", "policies", ["status"])

    op.create_table(
        "policy_versions",
        sa.Column("policy_version_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rules", JSON_DOCUMENT, nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.policy_id"],
            name="fk_policy_versions_policy_id_policies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("policy_version_id", name="pk_policy_versions"),
        sa.UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
    )
    op.create_index("ix_policy_versions_policy_id", "policy_versions", ["policy_id"])

    op.create_table(
        "rewards",
        sa.Column("reward_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("total_reward", sa.Float(), nullable=False),
        sa.Column("components", JSON_DOCUMENT, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["analyst_decisions.decision_id"],
            name="fk_rewards_decision_id_analyst_decisions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_rewards_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("reward_id", name="pk_rewards"),
    )
    op.create_index("ix_rewards_incident_id", "rewards", ["incident_id"])
    op.create_index("ix_rewards_created_at", "rewards", ["created_at"])

    op.create_table(
        "incident_memory",
        sa.Column("memory_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("outcome_tags", JSON_DOCUMENT, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_incident_memory_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("memory_id", name="pk_incident_memory"),
        sa.UniqueConstraint("incident_id", name="uq_incident_memory_incident_id"),
    )
    op.create_index("ix_incident_memory_updated_at", "incident_memory", ["updated_at"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_audit_events_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])
    op.create_index("ix_audit_events_timestamp", "audit_events", ["timestamp"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("incident_memory")
    op.drop_table("rewards")
    op.drop_table("policy_versions")
    op.drop_table("policies")
    op.drop_table("analyst_decisions")
    op.drop_table("agent_outputs")
    op.drop_table("evidence")
    op.drop_table("incident_segments")
    op.drop_table("fraud_scores")
    op.drop_table("incidents")
    op.drop_table("model_versions")
    op.drop_table("users")
    op.drop_table("transactions")
