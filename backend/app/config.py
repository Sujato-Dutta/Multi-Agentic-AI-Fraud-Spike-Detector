"""Typed configuration shared by offline and future online paths."""

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings; domain thresholds are configurable, never magic literals."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    model_dir: Path = Path("models")
    report_dir: Path = Path("reports")
    mlflow_tracking_uri: str = "sqlite:///mlflow.db"
    fraud_primary_model_path: Path = Path("models/fraud/fraud_model.joblib")
    fraud_fallback_model_path: Path = Path("models/fraud/isolation_forest.joblib")

    calibration_fraction: float = Field(0.10, gt=0, lt=0.5)
    validation_precision_floor: float = Field(0.90, gt=0, lt=1)
    random_seed: int = 20260822

    detector_window_minutes: int = Field(120, ge=30)
    detector_slide_minutes: int = Field(15, ge=1)
    detector_min_support: int = Field(20, ge=1)
    detector_min_high_risk_count: int = Field(4, ge=1)
    detector_lift_threshold: float = Field(2.5, gt=1)
    detector_extreme_lift: float = Field(5.0, gt=1)
    detector_alpha: float = Field(0.01, gt=0, lt=1)
    detector_confirm_windows: int = Field(2, ge=1)
    detector_promo_share_threshold: float = Field(0.30, ge=0, le=1)
    detector_promo_lift_margin: float = Field(0.50, ge=0)
    detector_ewma_half_life_hours: float = Field(24.0, gt=0)
    detector_baseline_days: int = Field(7, ge=1)
    detector_cooldown_minutes: int = Field(120, ge=0)
    detector_inactive_windows_to_close: int = Field(2, ge=1)
    detector_warmup_windows: int = Field(8, ge=1)
    event_match_grace_minutes: int = Field(30, ge=0)

    analyst_review_cost_inr: float = Field(250.0, ge=0)
    customer_friction_cost_inr: float = Field(40.0, ge=0)
    detection_delay_cost_per_hour_inr: float = Field(500.0, ge=0)
    exposure_loss_factor: float = Field(1.0, ge=0)
    action_effects_path: Path = Path("infrastructure/action_effects.yaml")
    reward_alpha: float = Field(1.0, ge=0)
    reward_beta: float = Field(1.0, ge=0)
    reward_gamma: float = Field(1.0, ge=0)
    reward_delta: float = Field(1.0, ge=0)
    reward_model_path: Path = Path("models/reward/reward_model.joblib")
    production_policy_path: Path = Path("models/policy/production_policy.joblib")
    candidate_policy_path: Path = Path("models/policy/candidate_policy.joblib")
    policy_holdback_fraction: float = Field(0.30, gt=0, lt=0.5)
    policy_reward_margin_inr: float = Field(0.0, ge=0)
    policy_recall_tolerance: float = Field(0.02, ge=0, le=1)
    policy_fp_cost_tolerance: float = Field(0.05, ge=0, le=1)
    reward_model_estimators: int = Field(200, ge=20, le=2000)
    linucb_alpha: float = Field(0.25, ge=0, le=5)

    database_url: str = "postgresql+asyncpg://fraud:fraud@localhost:5432/fraud_detector"
    database_auto_create: bool = False
    database_pool_size: int = Field(5, ge=1, le=50)
    database_max_overflow: int = Field(10, ge=0, le=100)

    jwt_secret_key: str = "replace-with-a-random-local-secret"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = Field(30, ge=1, le=1440)
    service_token: str = "replace-with-a-random-service-token"
    demo_analyst_username: str = "analyst"
    demo_analyst_password: str = "replace-with-analyst-password"
    demo_lead_analyst_username: str = "lead_analyst"
    demo_lead_analyst_password: str = "replace-with-lead-password"
    demo_admin_username: str = "admin"
    demo_admin_password: str = "replace-with-admin-password"

    redis_url: str = "redis://localhost:6379/0"
    cache_max_entries: int = Field(10_000, ge=100)
    cache_default_ttl_seconds: int = Field(300, ge=1)

    redpanda_bootstrap_servers: str = "localhost:19092"
    redpanda_client_id: str = "fraud-detector"
    redpanda_consumer_group: str = "fraud-detector-v1"
    redpanda_auto_offset_reset: str = "earliest"
    topic_transactions: str = "transactions"
    topic_fraud_scores: str = "fraud_scores"
    topic_spike_alerts: str = "spike_alerts"
    topic_incidents: str = "incidents"
    topic_agent_events: str = "agent_events"
    topic_analyst_actions: str = "analyst_actions"
    topic_responses: str = "responses"
    topic_outcomes: str = "outcomes"
    topic_rewards: str = "rewards"
    topic_alerts: str = "alerts"
    stream_consumer_enabled: bool = True
    stream_max_poll_records: int = Field(500, ge=1, le=10_000)
    stream_poll_timeout_ms: int = Field(1000, ge=100, le=60_000)
    outbox_batch_size: int = Field(100, ge=1, le=1000)
    outbox_lease_seconds: int = Field(30, ge=5, le=600)
    outbox_poll_seconds: float = Field(1.0, ge=0.1, le=60)
    outbox_publish_timeout_seconds: float = Field(15.0, ge=1, le=120)
    outbox_cycle_retry_max_seconds: float = Field(60.0, ge=0.1, le=600)
    outbox_cycle_log_interval_seconds: float = Field(60.0, ge=1, le=3600)
    max_ingest_batch_size: int = Field(1000, ge=1, le=10_000)

    gemini_api_key: str = ""
    gemini_primary_model: str = "gemini-3.5-flash-lite"
    gemini_secondary_model: str = "gemini-3.1-flash-lite"
    gemini_economy_model: str = "gemma-4-31b-it"
    llm_timeout_seconds: float = Field(12.0, gt=0, le=120)
    llm_max_attempts: int = Field(2, ge=1, le=5)
    llm_circuit_failure_threshold: int = Field(3, ge=1, le=20)
    llm_circuit_reset_seconds: int = Field(60, ge=1, le=3600)
    llm_cache_ttl_seconds: int = Field(900, ge=1)
    llm_primary_input_cost_per_million: float = Field(0.0, ge=0)
    llm_primary_output_cost_per_million: float = Field(0.0, ge=0)
    llm_secondary_input_cost_per_million: float = Field(0.0, ge=0)
    llm_secondary_output_cost_per_million: float = Field(0.0, ge=0)
    llm_economy_input_cost_per_million: float = Field(0.0, ge=0)
    llm_economy_output_cost_per_million: float = Field(0.0, ge=0)
    investigation_false_positive_cost_inr: float = Field(40.0, ge=0)
    investigation_auto_start: bool = True
    checkpoint_database_url: str = ""
    policy_path: Path = Path("infrastructure/policies.yaml")
    stream_lag_alert_threshold: int = Field(1000, ge=1)
    drift_psi_alert_threshold: float = Field(0.20, gt=0, le=5)
    drift_psi_buckets: int = Field(10, ge=4, le=50)
    drift_window_transactions: int = Field(2_000, ge=100, le=100_000)
    frontend_dir: Path = Path("frontend")
    heldout_report_path: Path = Path("reports/heldout_test/results.json")
    langsmith_tracing: bool = False
    langsmith_project: str = "fraud-spike-detector-development"
    langsmith_api_key: str = ""

    @model_validator(mode="after")
    def reject_placeholder_secrets_outside_local_environments(self) -> Self:
        if self.app_env.lower() in {"development", "local", "test", "testing"}:
            return self
        credentials = {
            "JWT_SECRET_KEY": self.jwt_secret_key,
            "SERVICE_TOKEN": self.service_token,
            "DEMO_ANALYST_PASSWORD": self.demo_analyst_password,
            "DEMO_LEAD_ANALYST_PASSWORD": self.demo_lead_analyst_password,
            "DEMO_ADMIN_PASSWORD": self.demo_admin_password,
        }
        invalid = [
            name
            for name, value in credentials.items()
            if not value or value.startswith("replace-with-")
        ]
        if invalid:
            raise ValueError(
                f"Non-local environments require configured credentials: {', '.join(invalid)}"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
