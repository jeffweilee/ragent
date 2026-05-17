"""T7.5a — Composition root: wires all singletons and exports Container (B30)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

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
    chunks_index_name: str  # ES index for the chat retrieval / dual-write
    # B54/B55 T-FB.8 — feedback retrieval signal (renumbered from B50/B51)
    feedback_repository: Any  # FeedbackRepository | None
    feedback_hmac_secret: str | None  # None when CHAT_FEEDBACK_ENABLED=false


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
    from ragent.pipelines.chat import build_retrieval_pipeline
    from ragent.pipelines.factory import DocumentEmbedder, build_ingest_pipeline
    from ragent.plugins.registry import PluginRegistry
    from ragent.plugins.stub_graph import StubGraphExtractor
    from ragent.plugins.vector import VectorExtractor
    from ragent.repositories.document_repository import DocumentRepository
    from ragent.repositories.system_settings_repository import SystemSettingsRepository
    from ragent.services.active_model_registry import ActiveModelRegistry
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService
    from ragent.storage.minio_registry import MinioSiteRegistry

    http = httpx.Client(timeout=60.0)
    auth_http = httpx.Client(timeout=10.0)  # dedicated client for token exchange (10 s per spec)
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
    es_verify_certs = os.environ.get("ES_VERIFY_CERTS", "true").lower() == "true"
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
    document_store = ElasticsearchDocumentStore(
        hosts=es_hosts,
        index=os.environ.get("ES_CHUNKS_INDEX", "chunks_v1"),
        verify_certs=es_verify_certs,
        basic_auth=es_basic_auth,
    )

    # MARIADB_DSN may use either pymysql:// or aiomysql:// — async engine needs aiomysql.
    from ragent.bootstrap.init_schema import to_async_dsn

    # pool_pre_ping reconnects transparently when the server closed an idle
    # connection; pool_recycle must stay below the server-side wait_timeout.
    engine = create_async_engine(
        to_async_dsn(_require("MARIADB_DSN")),
        pool_pre_ping=True,
        pool_recycle=_int_env("MARIADB_POOL_RECYCLE_SECONDS", 280),
    )

    doc_repo = DocumentRepository(engine=engine)

    rate_limiter = RateLimiter.from_env()

    registry = PluginRegistry()
    registry.register(
        VectorExtractor(
            repo=doc_repo,
            chunks={},  # v2: chunks live in ES; vector plugin is a no-op stub.
            embedder=embedding_client,
            es=es_client,
        )
    )
    registry.register(StubGraphExtractor())

    # B50 T-EM.21 — Embedding-model lifecycle plumbing.
    # SystemSettingsRepository sits over the same engine as DocumentRepository
    # (table `system_settings` from migration 009). ActiveModelRegistry caches
    # the four `embedding.*` rows with a TTL refresh; lifespan startup calls
    # `await registry.refresh()` so query/ingest paths never see a cold cache.
    chunks_index_name = os.environ.get("ES_CHUNKS_INDEX", "chunks_v1")
    system_settings_repo = SystemSettingsRepository(engine=engine)
    embedding_registry = ActiveModelRegistry(
        settings_repo=system_settings_repo,
        ttl_seconds=_int_env("EMBEDDING_REGISTRY_TTL_SECONDS", 10),
    )
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
        from ragent.pipelines.chat import _FeedbackMemoryRetriever
        from ragent.repositories.feedback_repository import FeedbackRepository

        feedback_repository = FeedbackRepository(engine)
        feedback_hmac_secret = _require("FEEDBACK_HMAC_SECRET")
        feedback_weight = _float_env("CHAT_FEEDBACK_RRF_WEIGHT", 0.5)
        feedback_retriever = _FeedbackMemoryRetriever(
            es_client=es_client,
            doc_repo=doc_repo,
            min_votes=_int_env("CHAT_FEEDBACK_MIN_VOTES", 3),
            half_life_days=_int_env("CHAT_FEEDBACK_HALF_LIFE_DAYS", 14),
            request_timeout=_float_env("ES_QUERY_TIMEOUT_SECONDS", 10.0),
        )

    retrieval_pipeline = build_retrieval_pipeline(
        document_store=document_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
        rerank_client=rerank_client,
        registry=embedding_registry,
        embed_query_callable=partial(_embed, query=True),
        feedback_retriever=feedback_retriever,
        feedback_weight=feedback_weight,
    )

    ingest_pipeline = build_ingest_pipeline(
        embedder=DocumentEmbedder(
            registry=embedding_registry,
            embed_callable=partial(_embed, query=False),
        ),
        document_store=document_store,
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
        feedback_repository=feedback_repository,
        feedback_hmac_secret=feedback_hmac_secret,
    )


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container
