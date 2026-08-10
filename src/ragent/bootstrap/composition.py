"""T7.5a — Composition root: wires all singletons and exports Container (B30)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import TYPE_CHECKING, Any

from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode
from ragent.services.attachment_ingest_service import ATTACHMENT_MAX_SIZE_BYTES_DEFAULT

if TYPE_CHECKING:
    from ragent.repositories.session_document_repository import SessionDocumentRepository
    from ragent.routers.brainagent import BrainAgentFactory
    from ragent.routers.chatagent_v3 import AgentFactory
    from ragent.services.attachment_context_resolver import AttachmentContextResolver
    from ragent.services.retrieve_v2_service import RetrieveV2Service
from ragent.utility.env import bool_env as _bool_env
from ragent.utility.env import float_env as _float_env
from ragent.utility.env import int_env as _int_env
from ragent.utility.env import require as _require

_K8S_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


@dataclass
class Container:
    token_managers: Any  # tuple[TokenManager, TokenManager, TokenManager] — LLM, Embedding, Rerank
    embedding_client: Any
    llm_client: Any
    rerank_client: Any
    minio_registry: Any
    es_client: Any
    engine: Any
    rate_limiter: Any
    doc_repo: Any
    registry: Any
    retrieval_pipeline: Any
    ingest_pipeline: Any
    rate_limit: int
    rate_limit_window: int
    http: Any  # shared httpx.Client for embedding/LLM/rerank; closed at shutdown
    auth_http: Any  # httpx.Client for token exchange (10s timeout); closed at shutdown
    unprotect_client: Any  # UnprotectClient | None — optional pre-pipeline file unprotection
    # B50 T-EM.21 — embedding-model lifecycle plumbing
    system_settings_repo: Any
    embedding_registry: Any  # ActiveModelRegistry — refresh() in lifespan startup
    embedding_lifecycle_service: Any  # EmbeddingLifecycleService — admin router backend
    chunks_index_name: str  # physical ES index name (write path / VectorExtractor / lifecycle)
    embed_fn: (
        Any  # (EmbeddingModelConfig, list[str]) -> list[list[float]] — used by backfill worker
    )
    # B54/B55 T-FB.8 — feedback retrieval signal (renumbered from B50/B51)
    feedback_repository: Any  # FeedbackRepository | None
    feedback_hmac_secret: str | None  # None when CHAT_FEEDBACK_ENABLED=false
    # T-SK — user-owned skill presets (always wired; core CRUD, no env gate).
    skill_service: Any  # SkillService
    # T-APL.5 — env-driven size limits routed through composition root
    ingest_inline_max_bytes: int
    ingest_file_max_bytes: int
    ingest_list_max_limit: int
    ingest_upload_max_bytes: int
    excerpt_max_chars: int
    # T8.5a / T-AM — joserfc-based JWT verifier (VerifyingTokenManager) for
    # inbound JWT verification. ``None`` for non-JWT auth modes (none /
    # user_header); set for jwt_header / jwt_prefer_header.
    auth_token_manager: Any = None
    # T-CA — chatagent proxy config. URLs are None when env vars are unset; the
    # app only registers routes whose URLs are configured.
    chatagent_api_url: str | None = None
    chatagent_sessionlist_api_url: str | None = None
    chatagent_session_api_url: str | None = None
    chatagent_ap_name: str = "ragent"
    chatagent_auth: str | None = None
    # T-CAv3R — resumable v3 stream buffer (Redis Stream). None disables
    # resumability (the v3 POST falls back to a connection-bound stream).
    chat_stream_store: Any = None
    # T-CAv3N — NATS publisher for live session-list status. Connected in the
    # lifespan; a no-op when NATS_SERVERS is unset (list degrades to snapshot-only).
    nats_publisher: Any = None
    # T-CAv3.DIP — (user_id, user_token) -> twp_ai.agent.Agent. None when v3 is
    # disabled (chatagent_api_url unset); set whenever v3 is enabled.
    chatagent_agent_factory: AgentFactory | None = None
    # T-BRAIN — ragent-brain upstream surface (/brainagent/v1). None disables the
    # whole surface (BRAIN_API_URL unset).
    brain_api_url: str | None = None
    brain_key: str | None = None
    brain_timeout: float = 30.0
    # T-BRAIN.DIP — user_id -> twp_ai.agent.Agent (BrainAgent(BrainCaller)).
    brain_agent_factory: BrainAgentFactory | None = None
    # T-PAT — Personal Access Token slice. None when PAT_PUBLIC_KEY is unset
    # (the /pat/v1 router is not mounted and the /brainagent/v1 run path attaches
    # no PAT). The run path is the only consumer — see T-PAT.29.
    pat_service: Any = None
    pat_upstream_header_name: str = "X-Pat-Token"
    # JWKS verifier for the caller-supplied X-Id-Token on POST /pat/v1/authorize.
    # Set whenever pat_service is; separate from auth_token_manager because the
    # PAT slice needs one even in trust-header modes.
    pat_id_token_manager: Any = None
    # T-CAT — ingest-backed file attachments (ingest pipeline, session_documents).
    session_document_repo: SessionDocumentRepository | None = None
    attachment_context_resolver: AttachmentContextResolver | None = None
    retrieve_v2_service: RetrieveV2Service | None = None
    attachment_max_size_bytes: int = ATTACHMENT_MAX_SIZE_BYTES_DEFAULT
    # T-CAT.W16 — cap on how many attachment_ids a single /chatagent/v3 turn
    # may resolve (one DB round-trip per id in AttachmentContextResolver).
    attachment_max_files: int = 10
    # Heartbeat uses a sync callable + separate sync engine (not the AsyncEngine)
    # to avoid cross-loop issues when the thread starts its own event loop.
    heartbeat_tick: Any = None  # Callable[[str], None]
    heartbeat_interval: float = 10.0
    # Startup sweep + maintenance loop thresholds.
    pending_stale_seconds: int = 300
    uploaded_stale_seconds: int = 300
    deleting_stale_seconds: int = 300
    max_attempts: int = 5
    maintenance_interval_seconds: int = 300
    dispatcher: Any = None  # TaskiqDispatcher — used by startup sweep + maintenance loop


def _build_chatagent_agent_factory(
    http_client: Any,
    *,
    api_url: str,
    ap_name: str,
    auth: str | None,
    timeout: float,
) -> AgentFactory:
    """Assemble the (user_id, user_token) -> Agent closure for /chatagent/v3.

    The composition root is the only layer allowed to construct concrete
    Agent/Caller classes (DIP). ADKCaller carries per-request user/token
    state, so it cannot be a singleton instance like RagentCaller — the
    router instead receives this factory and calls it per request.
    """
    from twp_ai.agent import Agent
    from twp_ai.agents.adk import ADKAgent

    from ragent.clients.adk_caller import ADKCaller
    from ragent.services.attachment_context_resolver import AttachmentContext

    def factory(
        user_id: str, user_token: str, attachments: AttachmentContext | None = None
    ) -> Agent:
        caller = ADKCaller(
            http_client=http_client,
            api_url=api_url,
            ap_name=ap_name,
            user_id=user_id,
            user_token=user_token,
            auth=auth,
            timeout=timeout,
            attachments=attachments.files_json if attachments else None,
            attachments_instruction=attachments.instruction if attachments else None,
        )
        return ADKAgent(caller)

    return factory


def _build_brain_agent_factory(
    http_client: Any,
    *,
    brain_url: str,
    brain_key: str | None,
    timeout: float,
) -> BrainAgentFactory:
    """Assemble the user_id -> Agent closure for /brainagent/v1.

    The composition root is the only layer allowed to name the concrete
    BrainAgent/BrainCaller classes (DIP). BrainCaller carries the per-request
    X-User-Id, so it cannot be a singleton — the router receives this factory
    and calls it per request. brain speaks twp-ai natively, so BrainAgent is a
    passthrough relay (no ADK-style translation).
    """
    from twp_ai.agent import Agent
    from twp_ai.agents.brain import BrainAgent

    from ragent.clients.brain_caller import BrainCaller

    def factory(user_id: str, extra_headers: Mapping[str, str] | None = None) -> Agent:
        caller = BrainCaller(
            http_client=http_client,
            brain_url=brain_url,
            user_id=user_id,
            brain_key=brain_key,
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return BrainAgent(caller)

    return factory


def _make_heartbeat_tick(sync_engine: Any) -> Any:
    from sqlalchemy import text

    def tick(document_id: str) -> None:
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE documents SET updated_at=NOW(6) WHERE document_id=:id"),
                {"id": document_id},
            )

    return tick


def build_container() -> Container:
    import httpx
    from elasticsearch import Elasticsearch
    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore
    from sqlalchemy.ext.asyncio import create_async_engine

    from ragent.bootstrap.http_logging import install_error_logging
    from ragent.clients.auth import TokenManager
    from ragent.clients.embedding import EmbeddingClient
    from ragent.clients.embedding_model_config import EmbeddingModelConfig
    from ragent.clients.llm import LLMClient
    from ragent.clients.rate_limiter import RateLimiter
    from ragent.clients.rerank import RerankClient
    from ragent.extractors.registry import PluginRegistry
    from ragent.extractors.stub_graph import StubGraphExtractor
    from ragent.extractors.vector import VectorExtractor
    from ragent.pipelines.ingest import DocumentEmbedder, build_ingest_pipeline
    from ragent.pipelines.retrieve import EXCERPT_MAX_CHARS_DEFAULT, build_retrieval_pipeline
    from ragent.repositories.document_repository import DocumentRepository
    from ragent.repositories.skill_repository import SkillRepository
    from ragent.repositories.system_settings_repository import SystemSettingsRepository
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.services.embedding.registry import ActiveModelRegistry
    from ragent.services.ingest_service import (
        FILE_MAX_BYTES_DEFAULT,
        INLINE_MAX_BYTES_DEFAULT,
        LIST_MAX_LIMIT_DEFAULT,
    )
    from ragent.services.skill_service import SkillService
    from ragent.storage.minio_registry import MinioSiteRegistry

    # Global outbound-TLS verify default. Each per-client verify env below falls
    # back to this, so RAGENT_TLS_VERIFY=false flips every upstream at once while
    # an explicit per-client env still overrides it. verify=False disables cert
    # validation (dev/self-signed only) — for prod with a private CA, keep it true
    # and mount the CA via SSL_CERT_FILE instead.
    tls_verify = _bool_env("RAGENT_TLS_VERIFY", True)
    http = httpx.Client(timeout=60.0, verify=tls_verify)
    auth_http = httpx.Client(timeout=10.0, verify=tls_verify)  # token exchange (10 s per spec)
    install_error_logging(http, client_name="upstream")
    install_error_logging(auth_http, client_name="auth", redact_auth_body=True)

    auth_url = _require("AI_API_AUTH_URL")
    use_k8s = _bool_env("AI_USE_K8S_SERVICE_ACCOUNT_TOKEN", False)

    join_mode = os.environ.get("CHAT_JOIN_MODE", "rrf")
    enable_rerank = _bool_env("CHAT_RERANK_ENABLED", True)

    if use_k8s:
        # Single SA token exchanged for J2; shared across all three services.
        _shared = TokenManager(
            auth_url=auth_url,
            j1_token=None,
            k8s_sa_token_path=_K8S_SA_TOKEN_PATH,
            http=auth_http,
        )
        llm_tm = embedding_tm = rerank_tm = _shared
    else:
        llm_tm = TokenManager(
            auth_url=auth_url, j1_token=_require("AI_LLM_API_J1_TOKEN"), http=auth_http
        )
        embedding_tm = TokenManager(
            auth_url=auth_url, j1_token=_require("AI_EMBEDDING_API_J1_TOKEN"), http=auth_http
        )
        # Only require rerank credentials when reranking is enabled.
        rerank_tm = (
            TokenManager(
                auth_url=auth_url,
                j1_token=_require("AI_RERANK_API_J1_TOKEN"),
                http=auth_http,
            )
            if enable_rerank
            else None
        )

    _bootstrap_embed_url = _require("EMBEDDING_API_URL")
    embedding_client = EmbeddingClient(
        api_url=_bootstrap_embed_url,
        http=http,
        get_token=embedding_tm.get_token,
    )

    llm_client = LLMClient(
        api_url=_require("LLM_API_URL"),
        http=http,
        get_token=llm_tm.get_token,
    )

    rerank_client = (
        RerankClient(
            api_url=_require("RERANK_API_URL"),
            http=http,
            get_token=rerank_tm.get_token,  # type: ignore[union-attr]
        )
        if enable_rerank
        else None
    )

    # v2: MinioSiteRegistry — fail-fast on missing __default__; falls back to
    # legacy single-MinIO env vars when MINIO_SITES is unset (synthesised entry).
    minio_registry = MinioSiteRegistry.from_env()

    es_hosts = _require("ES_HOSTS").split(",")
    es_verify_certs = _bool_env("ES_VERIFY_CERTS", tls_verify)
    _es_password = os.environ.get("ES_PASSWORD")
    es_basic_auth = (
        (os.environ.get("ES_USERNAME", "elastic"), _es_password)
        if _es_password is not None
        else None
    )
    es_client = Elasticsearch(
        hosts=es_hosts,
        basic_auth=es_basic_auth,
        verify_certs=es_verify_certs,
    )
    chunks_index_name = os.environ.get("ES_CHUNKS_INDEX", "chunks_v1")
    chunks_read_alias = f"{chunks_index_name}_active"
    document_store = ElasticsearchDocumentStore(
        hosts=es_hosts,
        index=chunks_read_alias,
        verify_certs=es_verify_certs,
        basic_auth=es_basic_auth,
    )

    # MARIADB_DSN may use either pymysql:// or aiomysql:// — async engine needs aiomysql.
    from ragent.bootstrap.init_schema import patch_aiomysql_ping, to_async_dsn, to_sync_dsn

    # pool_pre_ping reconnects transparently when the server closed an idle
    # connection; pool_recycle must stay below the server-side wait_timeout.
    engine = create_async_engine(
        to_async_dsn(_require("MARIADB_DSN")),
        pool_pre_ping=True,
        pool_recycle=_int_env("MARIADB_POOL_RECYCLE_SECONDS", 280),
    )
    patch_aiomysql_ping(engine)

    # T-ATTACH-R.0b — heartbeat uses a separate sync engine to avoid
    # cross-loop issues with the AsyncEngine (each heartbeat thread would
    # need its own event loop; sync engine sidesteps this entirely).
    from sqlalchemy import create_engine as _create_sync_engine

    _sync_engine = _create_sync_engine(
        to_sync_dsn(_require("MARIADB_DSN")),
        pool_pre_ping=True,
        pool_recycle=_int_env("MARIADB_POOL_RECYCLE_SECONDS", 280),
    )
    heartbeat_tick = _make_heartbeat_tick(_sync_engine)
    heartbeat_interval = _float_env("WORKER_HEARTBEAT_INTERVAL_SECONDS", 10.0)
    pending_stale_seconds = _int_env("MAINTENANCE_PENDING_STALE_SECONDS", 300)
    uploaded_stale_seconds = _int_env("MAINTENANCE_UPLOADED_STALE_SECONDS", 300)
    deleting_stale_seconds = _int_env("MAINTENANCE_DELETING_STALE_SECONDS", 300)
    max_attempts = _int_env("WORKER_MAX_ATTEMPTS", 5)
    maintenance_interval_seconds = _int_env("WORKER_MAINTENANCE_INTERVAL_SECONDS", 300)

    from ragent.bootstrap.broker import broker as _broker
    from ragent.bootstrap.dispatcher import TaskiqDispatcher

    dispatcher = TaskiqDispatcher(broker=_broker)

    doc_repo = DocumentRepository(engine=engine)

    # T-SK — user-owned skill presets. Core CRUD over the same engine (table
    # `skills` from migration 013); always wired (no env gate). The service is
    # injected into both the /skills router and the /chatagent/v3 router (skill
    # injection on a turn).
    skill_service = SkillService(SkillRepository(engine=engine))

    rate_limiter = RateLimiter.from_env()

    # B50 T-EM.21 — Embedding-model lifecycle plumbing.
    # SystemSettingsRepository sits over the same engine as DocumentRepository
    # (table `system_settings` from migration 009). ActiveModelRegistry caches
    # the four `embedding.*` rows with a TTL refresh; lifespan startup calls
    # `await registry.refresh()` so query/ingest paths never see a cold cache.
    # Constructed before VectorExtractor so that registry= can be injected
    # (B62 — delete() must fan out across stable + candidate indices during
    # CANDIDATE/CUTOVER lifecycle; see issue #147).
    system_settings_repo = SystemSettingsRepository(engine=engine)
    embedding_registry = ActiveModelRegistry(
        settings_repo=system_settings_repo,
        ttl_seconds=_int_env("EMBEDDING_REGISTRY_TTL_SECONDS", 10),
        chunks_read_alias=chunks_read_alias,
        chunks_fallback_index=chunks_index_name,
    )

    registry = PluginRegistry()
    registry.register(
        VectorExtractor(
            repo=doc_repo,
            chunks={},  # v2: extract() is a no-op; delete() uses delete_by_query by document_id.
            embedder=embedding_client,
            es=es_client,
            index=chunks_index_name,
            registry=embedding_registry,
        )
    )
    registry.register(StubGraphExtractor())

    embedding_lifecycle_service = EmbeddingLifecycleService(
        settings_repo=system_settings_repo,
        es_client=es_client,
        index_name=chunks_index_name,
        registry=embedding_registry,
        cache_ttl_seconds=_int_env("EMBEDDING_REGISTRY_TTL_SECONDS", 10),
    )

    # Per-model EmbeddingClient cache. Different candidate models can sit
    # behind different api_urls; cache by (api_url, model_arg) so two
    # promotes to the same endpoint reuse one HTTP-level client.
    _embed_cache: dict[tuple[str, str], EmbeddingClient] = {}

    def _client_for(model: EmbeddingModelConfig) -> EmbeddingClient:
        # Empty `api_url` in the seed row means "use the operator-provided
        # bootstrap env var". This is the common case for a fresh install:
        # the seed (migrations/009 / schema.sql) ships with `api_url=""`
        # and the operator sets `EMBEDDING_API_URL=...` in their .env.
        # Once they `/promote` a candidate they supply an explicit api_url,
        # which then takes precedence.
        url = model.api_url or _bootstrap_embed_url
        key = (url, model.model_arg)
        if key not in _embed_cache:
            _embed_cache[key] = EmbeddingClient(
                api_url=url,
                http=http,
                get_token=embedding_tm.get_token,
                model=model.model_arg,
            )
        return _embed_cache[key]

    from functools import partial

    def _embed(model: EmbeddingModelConfig, texts: list[str], *, query: bool) -> list[list[float]]:
        return _client_for(model).embed(texts, query=query)

    # B54/B55 T-FB.8 — Feedback retrieval signal (renumbered from B50/B51 after
    # collision with embedding-lifecycle B50). Ships dark; CHAT_FEEDBACK_ENABLED
    # gates both the /feedback router registration (bootstrap/app.py) and the
    # 3rd RRF retriever wired below.
    feedback_enabled = _bool_env("CHAT_FEEDBACK_ENABLED", False)
    feedback_hmac_secret: str | None = None
    feedback_repository = None
    feedback_retriever = None
    feedback_weight = 0.5
    if feedback_enabled:
        from ragent.pipelines.retrieve import _FeedbackMemoryRetriever
        from ragent.repositories.feedback_repository import FeedbackRepository

        feedback_repository = FeedbackRepository(engine)
        feedback_hmac_secret = _require("FEEDBACK_HMAC_SECRET")
        feedback_weight = _float_env("CHAT_FEEDBACK_RRF_WEIGHT", 0.5)
        feedback_retriever = _FeedbackMemoryRetriever(
            es_client=es_client,
            doc_repo=doc_repo,
            chunks_index=chunks_read_alias,
            min_votes=_int_env("CHAT_FEEDBACK_MIN_VOTES", 3),
            half_life_days=_int_env("CHAT_FEEDBACK_HALF_LIFE_DAYS", 14),
            request_timeout=_float_env("ES_QUERY_TIMEOUT_SECONDS", 10.0),
        )

    excerpt_max_chars = _int_env("EXCERPT_MAX_CHARS", EXCERPT_MAX_CHARS_DEFAULT)
    # Spec §4.6 ties the admin upload route's size ceiling to the same env knob
    # as inline ingest — share one read so the two Container fields cannot drift.
    inline_max_bytes = _int_env("INGEST_INLINE_MAX_BYTES", INLINE_MAX_BYTES_DEFAULT)
    # Shared by both the attachments router's cheap early check and
    # AttachmentIngestService's authoritative post-read check (mirrors above).
    attachment_max_size_bytes = _int_env(
        "ATTACHMENT_MAX_SIZE_BYTES", ATTACHMENT_MAX_SIZE_BYTES_DEFAULT
    )
    # T-CAT.W16 — cap on attachment_ids per /chatagent/v3 turn (each id costs
    # one DB round-trip in AttachmentContextResolver.resolve()).
    attachment_max_files = _int_env("ATTACHMENT_MAX_FILES", 10)
    retrieval_pipeline = build_retrieval_pipeline(
        document_store=document_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
        rerank_client=rerank_client,
        registry=embedding_registry,
        embed_query_callable=partial(_embed, query=True),
        feedback_retriever=feedback_retriever,
        feedback_weight=feedback_weight,
        excerpt_max_chars=excerpt_max_chars,
    )

    ingest_pipeline = build_ingest_pipeline(
        embedder=DocumentEmbedder(
            registry=embedding_registry,
            embed_callable=partial(_embed, query=False),
            es_client=es_client,
        ),
    )

    unprotect_client = None
    if _bool_env("UNPROTECT_ENABLED", False):
        from ragent.clients.unprotect import UnprotectClient

        unprotect_client = UnprotectClient(
            api_url=_require("UNPROTECT_API_URL"),
            apikey=_require("UNPROTECT_APIKEY"),
            delegated_user_suffix=_require("UNPROTECT_DELEGATED_USER_SUFFIX"),
            http=http,
            timeout=_float_env("UNPROTECT_TIMEOUT_SECONDS", 30.0),
        )

    # T-CAT — ingest-backed file attachments. Always-on: no env gate.
    from ragent.repositories.session_document_repository import SessionDocumentRepository
    from ragent.services.attachment_context_resolver import AttachmentContextResolver
    from ragent.services.retrieve_v2_service import RetrieveV2Service

    session_document_repo = SessionDocumentRepository(engine=engine)
    # AttachmentIngestService is built in app.py because it depends on IngestService
    # which requires the TaskIQ broker (only available in the app factory).
    attachment_context_resolver = AttachmentContextResolver(
        session_document_repo=session_document_repo,
        document_repo=doc_repo,
    )
    retrieve_v2_service = RetrieveV2Service(document_repo=doc_repo)

    # T8.5a / T-AM.2 — Build the joserfc-based JWKS verifier iff inbound JWT
    # auth is on. OIDC discovery + JWKS are fetched HERE (boot-time) so a
    # misconfigured OIDC_DOMAIN aborts startup rather than 500-ing the first
    # request; JWKS is then cached for the manager's lifetime (§3.5 cache-reuse).
    def _build_oidc_token_manager(*, verify_aud: bool, verify_exp: bool) -> Any:
        from ragent.auth.jwt import build_token_manager

        return build_token_manager(
            domain=_require("OIDC_DOMAIN"),
            audience=_require("OIDC_AUDIENCE"),
            use_https=_bool_env("OIDC_USE_HTTPS", True),
            verify_ssl=_bool_env("OIDC_VERIFY_SSL", tls_verify),
            verify_aud=verify_aud,
            verify_exp=verify_exp,
        )

    auth_token_manager: Any = None
    if parse_auth_mode() in (AuthMode.jwt_header, AuthMode.jwt_prefer_header):
        auth_token_manager = _build_oidc_token_manager(
            verify_aud=_bool_env("RAGENT_JWT_VERIFY_AUD", True),
            verify_exp=_bool_env("RAGENT_JWT_VERIFY_EXP", True),
        )

    chatagent_api_url = os.environ.get("CHATAGENT_API_URL") or None
    chatagent_sessionlist_api_url = os.environ.get("CHATAGENT_SESSIONLIST_API_URL") or None
    chatagent_session_api_url = os.environ.get("CHATAGENT_SESSION_API_URL") or None
    chatagent_ap_name = os.environ.get("CHATAGENT_AP_NAME", "ragent")
    chatagent_auth = os.environ.get("CHATAGENT_AUTH") or None
    brain_api_url = os.environ.get("BRAIN_API_URL") or None
    brain_key = os.environ.get("BRAIN_KEY") or None
    brain_timeout = _float_env("BRAIN_TIMEOUT_SECONDS", 30.0)
    # Stand up the resumable-stream buffer + NATS status publisher when EITHER the
    # v3 or the brain chat surface is configured — both reuse the same singletons.
    # The publisher reads its config here (env seam) but connects later in the
    # lifespan (async); NATS_SERVERS unset → publish is a no-op (snapshot only).
    chat_stream_store = None
    nats_publisher = None
    chatagent_agent_factory = None
    brain_agent_factory = None
    if chatagent_api_url is not None or brain_api_url is not None:
        from ragent.clients.chat_stream_store import ChatStreamStore
        from ragent.clients.nats_publisher import NatsSessionPublisher

        chat_stream_store = ChatStreamStore.from_env()
        nats_publisher = NatsSessionPublisher(
            servers=os.environ.get("NATS_SERVERS") or None,
            auth_service_url=os.environ.get("NATS_AUTH_SERVICE_URL") or None,
            client_secret=os.environ.get("NATS_AUTH_CLIENT_SECRET") or None,
            namespace=os.environ.get("NATS_AUTH_NAMESPACE") or None,
            subject_template=os.environ.get(
                "NATS_SESSION_SUBJECT_TEMPLATE", "session.{user}.status"
            ),
            verify_certs=_bool_env("NATS_AUTH_VERIFY_CERTS", tls_verify),
            connect_timeout_seconds=_float_env("NATS_CONNECT_TIMEOUT_SECONDS", 10.0),
            jwt_refresh_seconds=_float_env("NATS_JWT_REFRESH_SECONDS", 30.0),
        )
        if chatagent_api_url is not None:
            chatagent_agent_factory = _build_chatagent_agent_factory(
                http,
                api_url=chatagent_api_url,
                ap_name=chatagent_ap_name,
                auth=chatagent_auth,
                timeout=_float_env("CHATAGENT_TIMEOUT_SECONDS", 30.0),
            )
        if brain_api_url is not None:
            brain_agent_factory = _build_brain_agent_factory(
                http,
                brain_url=brain_api_url,
                brain_key=brain_key,
                timeout=brain_timeout,
            )

    # T-PAT — Personal Access Token slice, feature-gated on PAT_PUBLIC_KEY. When
    # set, the PAT is verified against that static key, encrypted with the same
    # KeyManager DEK as attachments, stored one-per-user, cached in redis, and
    # rotated via the refresh service. The /pat/v1 router + the /brainagent/v1
    # PAT attach only exist when this is wired.
    pat_service = None
    pat_id_token_manager = None
    pat_upstream_header_name = os.environ.get("PAT_UPSTREAM_HEADER_NAME", "X-Pat-Token")
    pat_public_key = os.environ.get("PAT_PUBLIC_KEY") or None
    if pat_public_key is not None:
        from ragent.auth.pat_jwt import PatTokenVerifier, import_pat_public_key
        from ragent.clients.pat_cache import PatCache
        from ragent.clients.pat_init_client import PatInitClient
        from ragent.clients.pat_refresh_client import PatRefreshClient
        from ragent.repositories.pat_repository import PatRepository
        from ragent.security.key_manager import KeyManager
        from ragent.security.pat_cipher import PATCipher
        from ragent.services.pat_service import PatService

        # `POST /pat/v1/authorize` verifies the caller-supplied `X-Id-Token`
        # itself, so the slice needs a JWKS verifier in EVERY auth mode — a
        # trust-header deployment builds none for the middleware. Reuse the
        # middleware's JWKS when it exists (no second discovery round-trip);
        # otherwise build one, which makes OIDC_DOMAIN / OIDC_AUDIENCE required
        # whenever the PAT slice is on.
        #
        # `aud`/`exp` verification is forced ON regardless of
        # RAGENT_JWT_VERIFY_AUD / _EXP: an id token's `aud` is OIDC_AUDIENCE
        # while an access token's is the resource server (`account` on a stock
        # Keycloak), so this check is what stops a caller pasting their ACCESS
        # token into X-Id-Token and getting an opaque 401 from init instead.
        # A dev flag loosening the middleware must not silently disable it.
        pat_id_token_manager = (
            _replace(auth_token_manager, verify_aud=True, verify_exp=True)
            if auth_token_manager is not None
            else _build_oidc_token_manager(verify_aud=True, verify_exp=True)
        )

        pat_alg = os.environ.get("PAT_JWT_ALG", "RS256")
        pat_key_manager = KeyManager(
            kek_b64=_require("RAGENT_KEK_BASE64"),
            encrypted_dek_b64=_require("RAGENT_ENCRYPTED_DEK_BASE64"),
        )
        # Resolved once: the refresh budget sizes the refresh client, the service's
        # retry loop AND the revocation tombstone (which must outlast the longest
        # in-flight refresh). Reading them in more than one place would let the
        # three drift apart.
        pat_refresh_timeout = _float_env("PAT_REFRESH_TIMEOUT_SECONDS", 30.0)
        pat_refresh_retries = _int_env("PAT_REFRESH_MAX_RETRIES", 3)
        pat_refresh_backoff = _float_env("PAT_REFRESH_BACKOFF_SECONDS", 0.5)
        pat_service = PatService(
            verifier=PatTokenVerifier(
                key=import_pat_public_key(pat_public_key, pat_alg),
                algorithm=pat_alg,
                expected_iss=_require("PAT_ISS"),
                expected_aud=_require("PAT_AUD"),
                nt_claim=_require("PAT_NT_KEY_NAME"),
            ),
            cipher=PATCipher(pat_key_manager),
            repo=PatRepository(engine=engine),
            cache=PatCache.from_env(
                tombstone_ttl_seconds=PatCache.tombstone_ttl_for(
                    pat_refresh_timeout, pat_refresh_retries, pat_refresh_backoff
                )
            ),
            refresh_client=PatRefreshClient(
                http,
                refresh_url=_require("PAT_REFRESH_API"),
                header_key=_require("PAT_API_HEADER_TOKEN_KEY"),
                header_value=_require("PAT_API_HEADER_TOKEN_VALUE"),
                timeout=pat_refresh_timeout,
            ),
            init_client=PatInitClient(
                http,
                init_url=_require("PAT_INIT_API_URL"),
                api_token_header_key=_require("PAT_INIT_API_TOKEN_HEADER_KEY_NAME"),
                api_token_value=_require("PAT_INIT_API_TOKEN"),
                authorize_header_key=_require("PAT_INIT_AUTHORIZE_HEADER_KEY_NAME"),
                sso_header_key=_require("PAT_INIT_SSO_HEADER_KEY_NAME"),
                sso_site_url=_require("PAT_INIT_SSO_SITE_URL"),
                expire_days=_int_env("PAT_INIT_EXPIRE_DAYS", 360),
                timeout=_float_env("PAT_INIT_TIMEOUT_SECONDS", 30.0),
            ),
            max_retries=pat_refresh_retries,
            backoff_base_seconds=pat_refresh_backoff,
        )

    return Container(
        token_managers=(llm_tm, embedding_tm, rerank_tm),
        embedding_client=embedding_client,
        llm_client=llm_client,
        rerank_client=rerank_client,
        minio_registry=minio_registry,
        es_client=es_client,
        engine=engine,
        rate_limiter=rate_limiter,
        doc_repo=doc_repo,
        registry=registry,
        retrieval_pipeline=retrieval_pipeline,
        ingest_pipeline=ingest_pipeline,
        rate_limit=_int_env("CHAT_RATE_LIMIT_PER_MINUTE", 60),
        rate_limit_window=_int_env("CHAT_RATE_LIMIT_WINDOW_SECONDS", 60),
        http=http,
        auth_http=auth_http,
        unprotect_client=unprotect_client,
        system_settings_repo=system_settings_repo,
        embedding_registry=embedding_registry,
        embedding_lifecycle_service=embedding_lifecycle_service,
        chunks_index_name=chunks_index_name,
        embed_fn=partial(_embed, query=False),
        feedback_repository=feedback_repository,
        feedback_hmac_secret=feedback_hmac_secret,
        skill_service=skill_service,
        ingest_inline_max_bytes=inline_max_bytes,
        ingest_file_max_bytes=_int_env("INGEST_FILE_MAX_BYTES", FILE_MAX_BYTES_DEFAULT),
        ingest_list_max_limit=_int_env("INGEST_LIST_MAX_LIMIT", LIST_MAX_LIMIT_DEFAULT),
        ingest_upload_max_bytes=inline_max_bytes,
        excerpt_max_chars=excerpt_max_chars,
        auth_token_manager=auth_token_manager,
        chatagent_api_url=chatagent_api_url,
        chatagent_sessionlist_api_url=chatagent_sessionlist_api_url,
        chatagent_session_api_url=chatagent_session_api_url,
        chatagent_ap_name=chatagent_ap_name,
        chatagent_auth=chatagent_auth,
        chat_stream_store=chat_stream_store,
        nats_publisher=nats_publisher,
        chatagent_agent_factory=chatagent_agent_factory,
        brain_api_url=brain_api_url,
        brain_key=brain_key,
        brain_timeout=brain_timeout,
        brain_agent_factory=brain_agent_factory,
        pat_service=pat_service,
        pat_id_token_manager=pat_id_token_manager,
        pat_upstream_header_name=pat_upstream_header_name,
        session_document_repo=session_document_repo,
        attachment_context_resolver=attachment_context_resolver,
        retrieve_v2_service=retrieve_v2_service,
        attachment_max_size_bytes=attachment_max_size_bytes,
        attachment_max_files=attachment_max_files,
        heartbeat_tick=heartbeat_tick,
        heartbeat_interval=heartbeat_interval,
        pending_stale_seconds=pending_stale_seconds,
        uploaded_stale_seconds=uploaded_stale_seconds,
        deleting_stale_seconds=deleting_stale_seconds,
        max_attempts=max_attempts,
        maintenance_interval_seconds=maintenance_interval_seconds,
        dispatcher=dispatcher,
    )


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container
