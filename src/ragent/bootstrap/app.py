"""T7.5c — FastAPI application factory: mounts all routers and middleware (B30)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.cors import CORSMiddleware

from ragent.auth.jwt import JwtAuthError, verify_jwt
from ragent.bootstrap.guard import enforce
from ragent.bootstrap.init_schema import init_schema
from ragent.bootstrap.logging_config import configure_logging
from ragent.bootstrap.metrics import (
    DocumentStatsCollector,
    make_document_stats_fetcher,
    setup_metrics,
)
from ragent.bootstrap.openapi import install_openapi, is_trust_header_mode
from ragent.bootstrap.telemetry import setup_tracing
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.middleware.logging import SCOPE_USER_ID_KEY, RequestLoggingMiddleware
from ragent.routers.admin_embedding import create_router as create_admin_embedding_router
from ragent.routers.admin_ingest import create_router as create_upload_ingest_router
from ragent.routers.chat import create_chat_router
from ragent.routers.feedback import create_feedback_router
from ragent.routers.health import create_health_router
from ragent.routers.ingest import create_router as create_ingest_router
from ragent.routers.mcp import create_mcp_router
from ragent.routers.retrieve import create_retrieve_router
from ragent.utility.env import bool_env, str_env
from ragent.utility.env import list_env as _list_env

logger = structlog.get_logger(__name__)


def _add_cors_middleware(app: FastAPI, origins: list[str]) -> None:
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_PUBLIC_PATHS = frozenset(
    {
        "/livez",
        "/readyz",
        "/startupz",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)
_DEFAULT_USER_ID_HEADER = "X-User-Id"
_DEFAULT_JWT_HEADER = "X-Auth-Token"
_DEFAULT_JWT_CLAIM = "preferred_username"

# Producer-side task labels that MUST be registered before traffic. Journal
# 2026-05-06 (B27): missing registration silently 500s on first dispatch.
_REQUIRED_TASK_LABELS = ("ingest.pipeline", "ingest.supersede")


async def _check_infra_ready(container: Any, broker: Any) -> None:
    """Verify DB, ES, and TaskIQ broker are ready before serving traffic.

    Raises ``RuntimeError`` on first failure so the lifespan aborts boot
    rather than silently degrading on first request.
    """
    from ragent.routers.health_probes import probe_es, probe_mariadb, run_probe

    db_failure = await run_probe("mariadb", probe_mariadb(container.engine))
    if db_failure is not None:
        raise RuntimeError(f"infra not ready: mariadb {db_failure.error_code}: {db_failure.detail}")

    es_failure = await run_probe("es", probe_es(container.es_client, index_names=[]))
    if es_failure is not None:
        raise RuntimeError(f"infra not ready: es {es_failure.error_code}: {es_failure.detail}")

    for label in _REQUIRED_TASK_LABELS:
        if broker.find_task(label) is None:
            raise RuntimeError(f"infra not ready: TaskIQ task not registered: {label!r}")

    # Pre-warm AI token exchange so a wrong AI_API_AUTH_URL or stale J1
    # surfaces as a boot abort instead of a first-request 500.
    for tm in container.token_managers:
        if tm is None:
            continue
        try:
            await anyio.to_thread.run_sync(tm.get_token)
        except Exception as exc:  # noqa: BLE001 — propagate as RuntimeError per probe contract
            raise RuntimeError(f"infra not ready: token exchange failed: {exc}") from exc


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


def _register_unhandled_exception_handler(app: FastAPI) -> None:
    """Register the catch-all `Exception` handler (00_rule.md §API Error Honesty).

    Domain exceptions that carry `error_code` / `http_status` attributes
    (e.g. `IngestStepError`, `UpstreamServiceError`, `UpstreamTimeoutError`)
    surface their values verbatim. Plain `Exception` instances fall back
    to 500 / `INTERNAL_ERROR`. The same `error_code` placed in the
    response body is the value logged on `api.unhandled`.
    """

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        error_code = getattr(exc, "error_code", None) or HttpErrorCode.INTERNAL_ERROR
        http_status = int(getattr(exc, "http_status", 500))
        if error_code == HttpErrorCode.INTERNAL_ERROR:
            title = "Internal server error"
        else:
            title = str(exc) or error_code
        logger.exception(
            "api.unhandled",
            path=request.url.path,
            method=request.method,
            error_code=error_code,
            http_status=http_status,
            error_type=type(exc).__name__,
        )
        return problem(http_status, error_code, title)


def _x_user_id_middleware(
    app: FastAPI,
    *,
    user_id_header: str = _DEFAULT_USER_ID_HEADER,
    jwt_header: str = _DEFAULT_JWT_HEADER,
    jwt_claim: str = _DEFAULT_JWT_CLAIM,
    trust_header: bool = True,
    auth_disabled: bool = True,
    token_manager: Any = None,
) -> None:
    """User-identity middleware (§3.5, rewritten 2026-05-20).

    Branch matrix:
      * ``auth_disabled=True`` (P1 default) OR ``trust_header=True`` (P2 dev override):
        require ``<user_id_header>`` non-empty; 422 ``MISSING_USER_ID`` otherwise.
      * ``auth_disabled=False`` AND ``trust_header=False`` (P2 prod): read
        ``<jwt_header>``, verify via joserfc against the OIDC JWKS, extract
        ``<jwt_claim>``, and inject the result into ``<user_id_header>`` on
        the request scope so downstream routers (whose ``Header(alias=...)``
        is bound to the canonical name) observe it transparently. Requires
        ``token_manager`` to be set (constructed by ``build_container``).
    """

    user_id_header_lower = user_id_header.lower().encode("latin-1")
    jwt_header_lower = jwt_header.lower()
    trust_header_mode = is_trust_header_mode(auth_disabled=auth_disabled, trust_header=trust_header)
    if not trust_header_mode and token_manager is None:
        raise RuntimeError(
            "JWT auth mode requires a token_manager; "
            "build_container() must construct one when "
            "RAGENT_AUTH_DISABLED=false and RAGENT_TRUST_X_USER_ID_HEADER=false."
        )

    def _inject_header(request: Request, value: str) -> None:
        """Make the resolved user_id visible to the downstream handler chain.

        Writes two channels:

        1. ``scope["headers"]`` — FastAPI's ``Header(alias=...)`` dependencies
           construct their own Request inside the route dispatcher and read
           from headers; the route layer must see the resolved value.
        2. ``scope[SCOPE_USER_ID_KEY]`` — Starlette's ``Headers(scope=...)``
           constructor **replaces** ``scope["headers"]`` with a fresh list per
           Request instance, so each ``BaseHTTPMiddleware`` boundary captures
           a different list reference. The outer ``RequestLoggingMiddleware``
           therefore cannot reliably observe header mutations done in inner
           middleware; the scope dict itself IS shared, so a dedicated dict
           key is the propagation channel for the outer log.
        """
        encoded = value.encode("latin-1")
        headers: list[tuple[bytes, bytes]] = [
            (name, val) for (name, val) in request.scope["headers"] if name != user_id_header_lower
        ]
        headers.append((user_id_header_lower, encoded))
        request.scope["headers"] = headers
        request.scope[SCOPE_USER_ID_KEY] = value

    @app.middleware("http")
    async def require_user_id(request: Request, call_next: Any) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if trust_header_mode:
            inbound = request.headers.get(user_id_header)
            if not inbound:
                logger.warning(
                    "api.user_id_missing",
                    path=request.url.path,
                    method=request.method,
                    error_code=HttpErrorCode.MISSING_USER_ID,
                    http_status=422,
                )
                return problem(
                    422, HttpErrorCode.MISSING_USER_ID, f"{user_id_header} header is required"
                )
            # The outer RequestLoggingMiddleware reads the canonical
            # ``X-User-Id`` only; a customised ``user_id_header`` is invisible
            # to it at request entry. The scope-key write feeds the final
            # ``api.request`` log (via ``_final_identity``); the contextvar
            # bind feeds handler-time structlog emissions.
            request.scope[SCOPE_USER_ID_KEY] = inbound
            structlog.contextvars.bind_contextvars(user_id=inbound)
            return await call_next(request)

        token = request.headers.get(jwt_header_lower) or ""
        try:
            user_id = verify_jwt(token, claim_user_id=jwt_claim, token_manager=token_manager)
        except JwtAuthError as exc:
            logger.warning(
                "api.jwt_invalid",
                path=request.url.path,
                method=request.method,
                error_code=exc.error_code,
                http_status=exc.http_status,
            )
            return problem(exc.http_status, exc.error_code, "Authentication failed")

        _inject_header(request, user_id)
        # Same contextvar binding as the trust-header branch — RequestLogging
        # Middleware reads X-User-Id from request.headers BEFORE call_next, so
        # in JWT mode the user id is absent at the outer layer; binding here
        # makes the JWT-derived id available to every subsequent log emission
        # (handler-time logs + the outer api.request log via merge_contextvars).
        structlog.contextvars.bind_contextvars(user_id=user_id)
        return await call_next(request)


_document_stats_registered = False


def _register_document_stats_collector() -> None:
    """Wire DocumentStatsCollector to MARIADB_DSN once per process.

    `prometheus_client.REGISTRY` is module-global; double-registration raises.
    `create_app()` is called multiple times in the integration test factory
    suite, so we guard with a module-level flag instead of unregistering.
    """
    global _document_stats_registered
    if _document_stats_registered:
        return
    import os

    from prometheus_client import REGISTRY

    dsn = os.environ.get("MARIADB_DSN", "")
    if not dsn:
        return  # tests / dev without DB — collector contributes zero series.
    # Driver-only swap so it works for any prefix (mysql+aiomysql,
    # mariadb+aiomysql, etc.). The async-only driver (aiomysql) cannot be
    # used from a sync sqlalchemy Engine — see make_document_stats_fetcher.
    sync_dsn = dsn.replace("+aiomysql", "+pymysql")
    fetcher = make_document_stats_fetcher(sync_dsn)
    REGISTRY.register(DocumentStatsCollector(fetch_rows=fetcher))
    _document_stats_registered = True


def _build_probes(container: Any) -> dict:
    from ragent.routers.health_probes import (
        probe_es,
        probe_mariadb,
        probe_minio,
        probe_redis,
    )

    probes: dict = {
        "mariadb": probe_mariadb(container.engine),
        "es": probe_es(container.es_client, index_names=[container.chunks_index_name]),
        "minio": probe_minio(container.minio_registry.default().client),
    }
    redis_client = getattr(container.rate_limiter, "_redis", None)
    if redis_client is not None:
        probes["redis_rate_limiter"] = probe_redis(redis_client)
    return probes


def create_app() -> FastAPI:  # pragma: no cover — composition root, tested by integration/e2e suite
    enforce()
    configure_logging("ragent-api")
    setup_tracing("ragent-api")

    # Importing the workers module triggers `@broker.task` decorator
    # registration so that `dispatcher.enqueue(label, ...)` can resolve
    # task labels at producer side (B25).
    import ragent.workers.backfill  # noqa: F401
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
        # B50 T-EM.21: warm the embedding-model registry so the first
        # ingest/chat after boot doesn't raise ActiveModelRegistryNotReady.
        # Refresh failures degrade to stale-warning per the registry's
        # contract — they don't abort boot.
        await container.embedding_registry.refresh()
        logger.info("api.startup.infra_ready", db=True, es=True, broker=True)
        try:
            yield
        finally:
            await _close_infra(container)
            try:
                await taskiq_broker.shutdown()
            except Exception:  # noqa: BLE001 — shutdown path; log and continue
                logger.warning("api.shutdown.broker_failed", exc_info=True)
            try:
                container.http.close()
            except Exception:  # noqa: BLE001 — shutdown path; log and continue
                logger.warning("api.shutdown.http_close_failed", exc_info=True)
            try:
                container.auth_http.close()
            except Exception:  # noqa: BLE001 — shutdown path; log and continue
                logger.warning("api.shutdown.auth_http_close_failed", exc_info=True)
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
        registry=container.registry,
        inline_max_bytes=container.ingest_inline_max_bytes,
        file_max_bytes=container.ingest_file_max_bytes,
        list_max_limit=container.ingest_list_max_limit,
    )

    app.include_router(create_ingest_router(svc=ingest_svc))
    app.include_router(
        create_upload_ingest_router(
            svc=ingest_svc, max_upload_bytes=container.ingest_upload_max_bytes
        )
    )
    app.include_router(
        create_chat_router(
            retrieval_pipeline=container.retrieval_pipeline,
            llm_client=container.llm_client,
            rate_limiter=container.rate_limiter,
            rate_limit=container.rate_limit,
            rate_limit_window=container.rate_limit_window,
            feedback_hmac_secret=container.feedback_hmac_secret,
            excerpt_max_chars=container.excerpt_max_chars,
        )
    )
    app.include_router(
        create_retrieve_router(
            retrieval_pipeline=container.retrieval_pipeline,
            excerpt_max_chars=container.excerpt_max_chars,
        )
    )
    if container.feedback_hmac_secret is not None:
        app.include_router(
            create_feedback_router(
                feedback_repository=container.feedback_repository,
                embedding_client=container.embedding_client,
                es_client=container.es_client,
                hmac_secret=container.feedback_hmac_secret,
            )
        )
    app.include_router(
        create_mcp_router(
            retrieval_pipeline=container.retrieval_pipeline,
            excerpt_max_chars=container.excerpt_max_chars,
        )
    )
    app.include_router(
        create_admin_embedding_router(
            service=container.embedding_lifecycle_service,
            snapshot_provider=container.embedding_registry.snapshot,
            broker=dispatcher,
        )
    )
    app.include_router(create_health_router(probes=_build_probes(container)))
    setup_metrics(app)
    _register_document_stats_collector()

    _register_unhandled_exception_handler(app)

    # Single env read so middleware and Swagger doc derive from the same
    # values — flipping any of these flips both at once (T8.D1).
    _user_id_header = str_env("RAGENT_USER_ID_HEADER", _DEFAULT_USER_ID_HEADER)
    _jwt_header = str_env("RAGENT_JWT_HEADER", _DEFAULT_JWT_HEADER)
    _trust_header = bool_env("RAGENT_TRUST_X_USER_ID_HEADER", False)
    _auth_disabled = bool_env("RAGENT_AUTH_DISABLED", False)
    _x_user_id_middleware(
        app,
        user_id_header=_user_id_header,
        jwt_header=_jwt_header,
        jwt_claim=str_env("RAGENT_JWT_CLAIM_USER_ID", _DEFAULT_JWT_CLAIM),
        trust_header=_trust_header,
        auth_disabled=_auth_disabled,
        token_manager=container.auth_token_manager,
    )
    install_openapi(
        app,
        auth_disabled=_auth_disabled,
        trust_header=_trust_header,
        user_id_header=_user_id_header,
        jwt_header=_jwt_header,
        public_paths=_PUBLIC_PATHS,
    )
    # CORSMiddleware is registered after _x_user_id_middleware so it runs
    # BEFORE the user-ID check (Starlette wraps in reverse order). This lets
    # CORS preflight OPTIONS requests short-circuit before hitting the 422 gate.
    _add_cors_middleware(app, _list_env("CORS_ALLOW_ORIGINS"))
    # RequestLoggingMiddleware is registered last so it runs FIRST (outermost),
    # capturing every request including preflight and missing-X-User-Id 422s.
    app.add_middleware(RequestLoggingMiddleware)

    return app
