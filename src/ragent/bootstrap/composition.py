"""T7.5a — Composition root: wires all singletons and exports Container (B30)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any


def _require(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        print(f"[ragent] required env var {var!r} is not set", file=sys.stderr)
        sys.exit(1)
    return val


def _float_env(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be a float, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def _int_env(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


@dataclass
class Container:
    token_manager: Any
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


def build_container() -> Container:
    import httpx
    import nltk
    from elasticsearch import Elasticsearch

    # Punkt is required by DocumentSplitter (EN sentence tokenizer). Download
    # once at startup so workers don't pay the cost per task and so airgapped
    # environments can pre-vendor the dataset.
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

    token_manager = TokenManager(
        auth_url=_require("AUTH_URL"),
        client_id=_require("AUTH_CLIENT_ID"),
        client_secret=_require("AUTH_CLIENT_SECRET"),
        http=http,
    )

    embedding_client = EmbeddingClient(
        api_url=_require("EMBEDDING_API_URL"),
        http=http,
        get_token=token_manager.get_token,
    )

    llm_client = LLMClient(
        api_url=_require("LLM_API_URL"),
        http=http,
        get_token=token_manager.get_token,
    )

    rerank_client = RerankClient(
        api_url=_require("RERANK_API_URL"),
        http=http,
        get_token=token_manager.get_token,
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
    es_client = Elasticsearch(
        hosts=es_hosts,
        basic_auth=(
            os.environ.get("ES_USERNAME", "elastic"),
            os.environ.get("ES_PASSWORD", ""),
        ),
        verify_certs=es_verify_certs,
    )
    document_store = ElasticsearchDocumentStore(
        hosts=es_hosts,
        index=os.environ.get("ES_CHUNKS_INDEX", "chunks_v1"),
        verify_certs=es_verify_certs,
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

    join_mode = os.environ.get("CHAT_JOIN_MODE", "rrf")
    retrieval_pipeline = build_retrieval_pipeline(
        embedder=embedding_client,
        document_store=document_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
    )

    ingest_pipeline = build_ingest_pipeline(
        embedder=DocumentEmbedder(embedding_client),
        document_store=document_store,
        chunk_repo=chunk_repo,
    )

    return Container(
        token_manager=token_manager,
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
    )


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container
