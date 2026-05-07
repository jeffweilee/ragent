"""T7.5c — FastAPI application factory: mounts all routers and middleware (B30)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response

from ragent.bootstrap.guard import enforce
from ragent.bootstrap.init_schema import init_schema
from ragent.bootstrap.logging_config import configure_logging
from ragent.bootstrap.telemetry import setup_tracing
from ragent.errors.problem import problem
from ragent.middleware.logging import RequestLoggingMiddleware
from ragent.routers.chat import create_chat_router
from ragent.routers.health import create_health_router
from ragent.routers.ingest import create_router as create_ingest_router
from ragent.routers.mcp import create_mcp_router
from ragent.routers.retrieve import create_retrieve_router

logger = structlog.get_logger(__name__)

_NO_USER_ID_PATHS = frozenset({"/livez", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"})

# Producer-side task labels that MUST be registered before traffic. Journal
# 2026-05-06 (B27): missing registration silently 500s on first dispatch.
_REQUIRED_TASK_LABELS = ("ingest.pipeline", "ingest.supersede")


async def _check_infra_ready(container: Any, broker: Any) -> None:
    """Verify DB, ES, and TaskIQ broker are ready before serving traffic.

    Raises ``RuntimeError`` on first failure so the lifespan aborts boot
    rather than silently degrading on first request.
    """
    from ragent.routers.health_probes import probe_es, probe_mariadb, run_probe

    db_failure = await run_probe(probe_mariadb(container.engine))
    if db_failure is not None:
        raise RuntimeError(f"infra not ready: mariadb {db_failure.error_code}: {db_failure.detail}")

    es_failure = await run_probe(probe_es(container.es_client, index_names=[]))
    if es_failure is not None:
        raise RuntimeError(f"infra not ready: es {es_failure.error_code}: {es_failure.detail}")

    for label in _REQUIRED_TASK_LABELS:
        if broker.find_task(label) is None:
            raise RuntimeError(f"infra not ready: TaskIQ task not registered: {label!r}")


async def _close_infra(container: Any) -> None:
    """Best-effort close of ES client and DB engine; never raises."""
    try:
        container.es_client.close()
    except Exception:  # noqa: BLE001 — shutdown path; log and continue
        logger.warning("api.shutdown.es_close_failed", exc_info=True)
    try:
        await container.engine.dispose()
    except Exception:  # noqa: BLE001 — shutdown path; log and continue
        logger.warning("api.shutdown.engine_dispose_failed", exc_info=True)


def _x_user_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_user_id(request: Request, call_next: Any) -> Response:
        if request.url.path in _NO_USER_ID_PATHS:
            return await call_next(request)
        if not request.headers.get("X-User-Id"):
            return problem(422, "MISSING_USER_ID", "X-User-Id header is required")
        return await call_next(request)


def _build_probes(container: Any) -> dict:
    from ragent.routers.health_probes import (
        probe_es,
        probe_mariadb,
        probe_minio,
        probe_redis,
    )

    probes: dict = {
        "mariadb": probe_mariadb(container.engine),
        "es": probe_es(container.es_client, index_names=["chunks_v1"]),
        "minio": probe_minio(container.minio_client),
    }
    redis_client = getattr(container.rate_limiter, "_redis", None)
    if redis_client is not None:
        probes["redis_rate_limiter"] = probe_redis(redis_client)
    return probes


def create_app() -> FastAPI:
    enforce()
    configure_logging("ragent-api")
    setup_tracing("ragent-api")

    # Importing the workers module triggers `@broker.task` decorator
    # registration so that `dispatcher.enqueue(label, ...)` can resolve
    # task labels at producer side (B25).
    import ragent.workers.ingest  # noqa: F401
    from ragent.bootstrap.broker import broker as taskiq_broker
    from ragent.bootstrap.composition import get_container
    from ragent.bootstrap.dispatcher import TaskiqDispatcher
    from ragent.services.ingest_service import IngestService

    container = get_container()
    # IngestService is async (post-aiomysql migration); FastAPI awaits it directly,
    # so the producer-side dispatcher must also be async to avoid blocking the loop.
    dispatcher = TaskiqDispatcher(taskiq_broker)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # TaskIQ producers must call `await broker.startup()` before
        # `kiq()` (B27). Failure here aborts boot — surfacing through
        # /readyz instead of silently 500-ing on first ingest.
        await taskiq_broker.startup()
        init_schema()
        await _check_infra_ready(container, taskiq_broker)
        logger.info("api.startup.infra_ready", db=True, es=True, broker=True)
        try:
            yield
        finally:
            await _close_infra(container)
            await taskiq_broker.shutdown()
            container.http.close()
            container.auth_http.close()
            import ragent.bootstrap.composition as _comp

            _comp._container = None  # noqa: SLF001 — prevent reuse of closed clients
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            if hasattr(provider, "shutdown"):
                provider.shutdown()

    app = FastAPI(title="ragent", lifespan=lifespan)

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)

    ingest_svc = IngestService(
        repo=container.doc_repo,
        storage=container.minio_registry,
        broker=dispatcher,
    )

    app.include_router(create_ingest_router(svc=ingest_svc))
    app.include_router(
        create_chat_router(
            retrieval_pipeline=container.retrieval_pipeline,
            llm_client=container.llm_client,
            rate_limiter=container.rate_limiter,
            rate_limit=container.rate_limit,
            rate_limit_window=container.rate_limit_window,
        )
    )
    app.include_router(create_retrieve_router(retrieval_pipeline=container.retrieval_pipeline))
    app.include_router(create_mcp_router())
    app.include_router(create_health_router(probes=_build_probes(container)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        logger.exception(
            "api.unhandled",
            path=request.url.path,
            method=request.method,
            error_code="INTERNAL_ERROR",
            error_type=type(exc).__name__,
        )
        return problem(500, "INTERNAL_ERROR", "Internal server error")

    _x_user_id_middleware(app)
    # RequestLoggingMiddleware is registered after _x_user_id_middleware so that
    # it runs FIRST (Starlette wraps middleware in reverse order). This way the
    # api.request log captures the missing-X-User-Id 422 too.
    app.add_middleware(RequestLoggingMiddleware)

    return app
