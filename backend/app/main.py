"""FastAPI application and Phase 4 runtime lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import InMemorySaver
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.exc import SQLAlchemyError
from structlog.contextvars import bind_contextvars, clear_contextvars

from backend.app.agents.graph import build_investigation_graph
from backend.app.api.router import router
from backend.app.api.websocket import WebSocketHub
from backend.app.cache.cache_service import CacheService
from backend.app.config import Settings, get_settings
from backend.app.core.logging import configure_logging
from backend.app.core.runtime import AppError
from backend.app.db.session import (
    SessionFactory,
    close_database,
    initialize_database,
)
from backend.app.hitl.feedback_service import FeedbackService
from backend.app.hitl.review_service import ReviewService
from backend.app.llm.gateway import StructuredLLMGateway
from backend.app.ml.reward.reward_model import RewardModel
from backend.app.monitoring.drift import DriftMonitor
from backend.app.monitoring.prometheus import observe_dependencies
from backend.app.safety.metrics import record_degradation
from backend.app.safety.policy_engine import PolicyEngine
from backend.app.services.demo_stream_service import DemoStreamService
from backend.app.services.evaluation_service import (
    ActionEffects,
    EvaluationService,
    RewardCalculator,
)
from backend.app.services.incident_service import IncidentService
from backend.app.services.investigation_service import InvestigationService
from backend.app.services.policy_service import PolicyService, RuntimePolicyResolver
from backend.app.services.transaction_service import TransactionService
from backend.app.streaming.consumer import EventConsumer
from backend.app.streaming.outbox import OutboxDispatcher
from backend.app.streaming.producer import EventProducer
from backend.app.streaming.topics import TopicSet

logger = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: Any = SessionFactory,
    cache: CacheService | None = None,
    transaction_service: TransactionService | None = None,
    investigation_service: InvestigationService | None = None,
    demo_stream_service: DemoStreamService | None = None,
    review_service: ReviewService | None = None,
    feedback_service: FeedbackService | None = None,
    policy_engine: PolicyEngine | None = None,
    checkpointer: Any | None = None,
    gateway: StructuredLLMGateway | None = None,
) -> FastAPI:
    config = settings or get_settings()
    configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        hub = WebSocketHub()
        cache_service = cache or CacheService(settings=config)
        runtime_state = cache_service.state
        active_policy = policy_engine or PolicyEngine.from_yaml(config.policy_path)
        try:
            action_effects = ActionEffects.from_yaml(config.action_effects_path)
            reward_calculator = RewardCalculator(action_effects, config)
        except (OSError, ValueError) as exc:
            runtime_state.mark_down(
                "reward_model", f"{type(exc).__name__}: {exc}"[:500]
            )
            runtime_state.mark_down(
                "response_policy", f"{type(exc).__name__}: {exc}"[:500]
            )
            raise AppError(
                "action_effects_invalid",
                503,
                "Versioned action-effect assumptions are unavailable",
            ) from exc
        try:
            await asyncio.to_thread(
                RewardModel.load,
                config.reward_model_path,
                assumptions_version=action_effects.version,
            )
        except FileNotFoundError:
            runtime_state.mark_degraded("reward_model", "reward_model_artifact_missing")
        except Exception as exc:  # noqa: BLE001 - optional model never blocks arithmetic
            runtime_state.mark_down(
                "reward_model", f"{type(exc).__name__}: {exc}"[:500]
            )
        else:
            runtime_state.mark_healthy("reward_model")
        response_policy = RuntimePolicyResolver(
            session_factory=session_factory,
            assumptions_version=action_effects.version,
            state=runtime_state,
        )
        await cache_service.connect()
        producer: EventProducer | None = None
        consumer: EventConsumer | None = None
        outbox_dispatcher: OutboxDispatcher | None = None
        consumer_task: asyncio.Task[None] | None = None
        outbox_task: asyncio.Task[None] | None = None
        checkpoint_context: Any | None = None
        if config.database_auto_create and session_factory is SessionFactory:
            await initialize_database()
        policy_healthy, policy_reason = await response_policy.validate_active()
        if policy_healthy:
            runtime_state.mark_healthy("response_policy")
        else:
            runtime_state.mark_degraded(
                "response_policy", policy_reason or "conservative_fixed_policy_active"
            )
        if config.stream_consumer_enabled:
            producer = EventProducer(config, state=runtime_state)
            try:
                await producer.start()
            except AppError as exc:
                logger.warning("stream_start_degraded", detail=exc.detail)
        active_investigation = investigation_service
        if active_investigation is None:
            active_checkpointer = checkpointer
            checkpoint_durable = active_checkpointer is not None and not isinstance(
                active_checkpointer, InMemorySaver
            )
            if active_checkpointer is None and config.database_url.startswith("postgresql"):
                checkpoint_url = config.checkpoint_database_url or config.database_url.replace(
                    "postgresql+asyncpg://", "postgresql://", 1
                )
                blocker = _checkpoint_loop_blocker()
                entered = False
                try:
                    if blocker is not None:
                        raise RuntimeError(blocker)
                    if config.checkpoint_database_url and not checkpoint_url.startswith(
                        ("postgresql://", "postgres://")
                    ):
                        raise ValueError(
                            "CHECKPOINT_DATABASE_URL must be a PostgreSQL connection URI; "
                            "HTTP(S) Supabase project URLs are not database connections"
                        )
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                    checkpoint_context = AsyncPostgresSaver.from_conn_string(checkpoint_url)
                    active_checkpointer = await checkpoint_context.__aenter__()
                    entered = True
                    await active_checkpointer.setup()
                    checkpoint_durable = True
                    runtime_state.mark_healthy("checkpoint")
                except Exception as exc:  # noqa: BLE001 - graph remains available in memory
                    reason = f"{type(exc).__name__}: {exc}"[:500]
                    active_checkpointer = None
                    checkpoint_durable = False
                    # Only unwind a context manager that actually entered. When __aenter__
                    # raises, the async generator has already terminated and athrow-ing into
                    # it raises RuntimeError, which previously escaped and killed startup.
                    if checkpoint_context is not None:
                        if entered:
                            try:
                                await checkpoint_context.__aexit__(
                                    type(exc), exc, exc.__traceback__
                                )
                            except Exception as cleanup_error:  # noqa: BLE001
                                logger.warning(
                                    "checkpoint_cleanup_failed",
                                    reason=f"{type(cleanup_error).__name__}: {cleanup_error}"[:500],
                                )
                        checkpoint_context = None
                    runtime_state.mark_degraded("checkpoint", reason)
                    logger.warning("checkpoint_store_degraded", reason=reason)
            if active_checkpointer is None:
                active_checkpointer = InMemorySaver()
                checkpoint_durable = False
                runtime_state.mark_degraded("checkpoint", "in_memory_checkpoint_store")
                record_degradation("checkpoint_unavailable")
            active_gateway = gateway or StructuredLLMGateway(
                cache_service, config, state=runtime_state
            )
            active_investigation = InvestigationService(
                build_investigation_graph(
                    active_gateway,
                    active_checkpointer,
                    active_policy,
                    response_policy,
                ),
                config,
                session_factory=session_factory,
                checkpoint_durable=checkpoint_durable,
                local_publisher=hub.broadcast,
            )
        topics = TopicSet.from_settings(config)
        active_evaluation = EvaluationService(
            session_factory=session_factory,
            calculator=reward_calculator,
            publisher=producer,
            topics=topics,
        )
        active_response_policy_service = PolicyService(
            session_factory=session_factory, settings=config
        )
        active_review = review_service or ReviewService(
            active_investigation,
            active_policy,
            session_factory=session_factory,
            publisher=producer,
            local_publisher=hub.broadcast,
            topics=topics,
        )
        active_feedback = feedback_service or FeedbackService(
            session_factory=session_factory,
            publisher=producer,
            local_publisher=hub.broadcast,
            topics=topics,
        )
        incident_service = IncidentService(
            config,
            session_factory=session_factory,
            publisher=producer,
            local_publisher=hub.broadcast,
            investigation_trigger=active_investigation.schedule,
        )
        service = transaction_service or TransactionService(
            config,
            cache=cache_service,
            session_factory=session_factory,
            publisher=producer,
            incident_service=incident_service,
            state=runtime_state,
        )
        service.incident_service.investigation_trigger = active_investigation.schedule
        await service.initialize()
        drift_monitor: DriftMonitor | None = None
        if service.reference_rows is not None:
            drift_monitor = DriftMonitor.from_training_reference(
                session_factory=session_factory,
                reference_frame=service.reference_rows,
                risk_probabilities=service.reference_rows["risk_probability"].to_numpy(
                    dtype=float
                ),
                settings=config,
            )
        if producer is not None:
            outbox_dispatcher = OutboxDispatcher(
                session_factory=session_factory,
                producer=producer,
                settings=config,
                state=runtime_state,
            )
            outbox_task = asyncio.create_task(
                outbox_dispatcher.run(), name="event-outbox-dispatcher"
            )
            topics = TopicSet.from_settings(config)
            consumer = EventConsumer(
                {
                    topics.transactions: service.handle_stream_event,
                    topics.outcomes: active_evaluation.handle_outcome,
                },
                config,
                state=runtime_state,
            )
            try:
                await consumer.start()
            except Exception as exc:  # noqa: BLE001 - API continues while stream is visibly down
                runtime_state.mark_down("stream", f"{type(exc).__name__}: {exc}"[:500])
                logger.warning("stream_consumer_degraded", reason=str(exc))
            else:
                consumer_task = asyncio.create_task(consumer.run(), name="redpanda-consumer")
        elif not config.stream_consumer_enabled:
            runtime_state.mark_degraded("stream", "consumer_disabled_by_configuration")

        active_demo_stream = demo_stream_service or DemoStreamService(
            config,
            producer=producer,
            cache=cache_service,
            session_factory=session_factory,
            runtime_reset=service.reset,
        )
        application.state.settings = config
        application.state.session_factory = session_factory
        application.state.cache = cache_service
        application.state.degradation_state = runtime_state
        application.state.producer = producer
        application.state.consumer = consumer
        application.state.outbox_dispatcher = outbox_dispatcher
        application.state.transaction_service = service
        application.state.investigation_service = active_investigation
        application.state.review_service = active_review
        application.state.feedback_service = active_feedback
        application.state.evaluation_service = active_evaluation
        application.state.response_policy_service = active_response_policy_service
        application.state.shadow_policy = response_policy
        application.state.policy_engine = active_policy
        application.state.websocket_hub = hub
        application.state.drift_monitor = drift_monitor
        application.state.demo_stream_service = active_demo_stream
        observe_dependencies(runtime_state.snapshot())
        try:
            yield
        finally:
            await active_demo_stream.close()
            await active_investigation.close()
            if consumer_task is not None:
                consumer_task.cancel()
                await asyncio.gather(consumer_task, return_exceptions=True)
            if consumer is not None:
                await consumer.stop()
            if outbox_dispatcher is not None:
                await outbox_dispatcher.stop()
            if outbox_task is not None:
                await asyncio.gather(outbox_task, return_exceptions=True)
            if producer is not None:
                await producer.stop()
            if checkpoint_context is not None:
                await checkpoint_context.__aexit__(None, None, None)
            await cache_service.close()
            if session_factory is SessionFactory:
                await close_database()

    application = FastAPI(
        title="Multi-Agentic AI Fraud Spike Detector",
        version="0.3.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def trace_requests(request: Request, call_next):
        trace_id = request.headers.get("x-trace-id", str(uuid4()))
        bind_contextvars(trace_id=trace_id)
        try:
            response = await call_next(request)
            response.headers["x-trace-id"] = trace_id
            path = request.url.path
            local_environment = config.app_env.lower() in {
                "development",
                "local",
                "test",
                "testing",
            }
            sensitive_demo_path = path in {
                "/api/auth/demo-credentials",
                "/api/demo/stream",
            }
            frontend_path = not (
                path == "/api" or path.startswith("/api/") or path == "/metrics"
            )
            if sensitive_demo_path or (local_environment and frontend_path):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            clear_contextvars()

    @application.exception_handler(AppError)
    async def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content={"error": error.to_dict()})

    @application.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        reason = f"{type(error).__name__}: {error}"[:500]
        request.app.state.degradation_state.mark_down("postgres", reason)
        record_degradation("postgres_down")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "database_unavailable",
                    "detail": "Postgres is unavailable; retry this write safely",
                }
            },
        )

    application.include_router(router)
    Instrumentator().instrument(application).expose(application, endpoint="/metrics")
    _mount_frontend(application, config)
    return application


def _checkpoint_loop_blocker() -> str | None:
    """Detect an event loop psycopg cannot use, before attempting a confusing connection.

    psycopg's async driver requires a selector-based loop. Windows defaults to
    ProactorEventLoop, so durable checkpointing needs the app started through
    ``scripts/run_api.py`` (or any entry point that installs the selector policy first).
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if type(loop).__name__ == "ProactorEventLoop":
        return (
            "psycopg cannot use ProactorEventLoop; start the API with "
            "'python scripts/run_api.py' to install a selector event loop and enable "
            "durable Postgres checkpoints"
        )
    return None


def _mount_frontend(application: FastAPI, config: Settings) -> None:
    """Serve the command center when present; a missing bundle must not break the API."""

    directory = config.frontend_dir
    if not directory.is_dir() or not (directory / "index.html").is_file():
        logger.warning("frontend_assets_missing", directory=str(directory))
        return
    application.mount(
        "/", StaticFiles(directory=str(directory), html=True), name="frontend"
    )


app = create_app()
