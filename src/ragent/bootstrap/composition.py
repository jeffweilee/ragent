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
    feedback_repository: Any  # FeedbackRepository | None (B50/B51, T-FB.8)
    feedback_hmac_secret: str | None  # None when CHAT_FEEDBACK_ENABLED=false
    feedback_enabled: bool


def build_container() -> Container:
    import httpx
    from elasticsearch import Elasticsearch
    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore
    from sqlalchemy.ext.asyncio import create_async_engine

    from ragent.bootstrap.http_logging import install_error_logging
    from ragent.clients.auth import TokenManager
    from ragent.clients.embedding import EmbeddingClient
    from ragent.clients.llm import LLMClient
    from ragent.clients.rate_limiter import RateLimiter
    from ragent.clients.rerank import RerankClient
    from ragent.pipelines.chat import build_retrieval_pipeline
    from ragent.pipelines.factory import DocumentEmbedder, build_ingest_pipeline
    from ragent.plugins.registry import PluginRegistry
    from ragent.plugins.stub_graph import StubGraphExtractor
    from ragent.plugins.vector import VectorExtractor
    from ragent.repositories.document_repository import DocumentRepository
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

    embedding_client = EmbeddingClient(
        api_url=_require("EMBEDDING_API_URL"),
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
        )

    retrieval_pipeline = build_retrieval_pipeline(
        embedder=embedding_client,
        document_store=document_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
        rerank_client=rerank_client,
        feedback_retriever=feedback_retriever,
        feedback_weight=feedback_weight,
    )

    ingest_pipeline = build_ingest_pipeline(
        embedder=DocumentEmbedder(embedding_client),
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
        feedback_repository=feedback_repository,
        feedback_hmac_secret=feedback_hmac_secret,
        feedback_enabled=feedback_enabled,
    )


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container
