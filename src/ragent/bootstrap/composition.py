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
    minio_client: Any
    es_client: Any
    engine: Any
    rate_limiter: Any
    doc_repo: Any
    chunk_repo: Any
    registry: Any
    retrieval_pipeline: Any
    ingest_pipeline: Any
    rate_limit: int
    rate_limit_window: int
    http: Any  # shared httpx.Client for embedding/LLM/rerank; closed at shutdown
    auth_http: Any  # httpx.Client for token exchange (10s timeout); closed at shutdown


def build_container() -> Container:
    import httpx
    import nltk
    from elasticsearch import Elasticsearch

    # Punkt is required by the chunker for EN sentence tokenization. Skip the
    # download (and its network call) when the dataset is already provisioned,
    # so airgapped environments can vendor it ahead of time.
    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        nltk.download("punkt_tab", quiet=True)

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore
    from minio import Minio
    from sqlalchemy import create_engine, text

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
    from ragent.repositories.chunk_repository import ChunkRepository
    from ragent.repositories.document_repository import DocumentRepository
    from ragent.storage.minio_client import MinIOClient

    http = httpx.Client(timeout=60.0)
    auth_http = httpx.Client(timeout=10.0)  # dedicated client for token exchange (10 s per spec)

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

    _minio_raw = Minio(
        endpoint=_require("MINIO_ENDPOINT"),
        access_key=_require("MINIO_ACCESS_KEY"),
        secret_key=_require("MINIO_SECRET_KEY"),
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )
    minio_client = MinIOClient(
        minio_client=_minio_raw,
        bucket=os.environ.get("MINIO_BUCKET", "ragent-uploads"),
        put_timeout=_float_env("MINIO_PUT_TIMEOUT_SECONDS", 30.0),
        get_timeout=_float_env("MINIO_GET_TIMEOUT_SECONDS", 30.0),
    )

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

    # SQLAlchemy `create_engine` returns an Engine wrapping a QueuePool by
    # default. Repos receive the Engine and check out a connection per call
    # (00_rule.md → Mandatory Connection Pool). The startup ping below uses
    # a transient checkout and releases it immediately.
    engine = create_engine(_require("MARIADB_DSN"))
    with engine.connect() as _ping:
        _ping.execute(text("SELECT 1"))

    doc_repo = DocumentRepository(engine=engine)
    chunk_repo = ChunkRepository(engine=engine)

    rate_limiter = RateLimiter.from_env()

    registry = PluginRegistry()
    registry.register(
        VectorExtractor(
            repo=doc_repo,
            chunks=chunk_repo,
            embedder=embedding_client,
            es=es_client,
        )
    )
    registry.register(StubGraphExtractor())

    retrieval_pipeline = build_retrieval_pipeline(
        embedder=embedding_client,
        document_store=document_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
        rerank_client=rerank_client,
    )

    ingest_pipeline = build_ingest_pipeline(
        embedder=DocumentEmbedder(embedding_client),
        document_store=document_store,
        chunk_repo=chunk_repo,
    )

    return Container(
        token_managers=(llm_tm, embedding_tm, rerank_tm),
        embedding_client=embedding_client,
        llm_client=llm_client,
        rerank_client=rerank_client,
        minio_client=minio_client,
        es_client=es_client,
        engine=engine,
        rate_limiter=rate_limiter,
        doc_repo=doc_repo,
        chunk_repo=chunk_repo,
        registry=registry,
        retrieval_pipeline=retrieval_pipeline,
        ingest_pipeline=ingest_pipeline,
        rate_limit=_int_env("CHAT_RATE_LIMIT_PER_MINUTE", 60),
        rate_limit_window=_int_env("CHAT_RATE_LIMIT_WINDOW_SECONDS", 60),
        http=http,
        auth_http=auth_http,
    )


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container
