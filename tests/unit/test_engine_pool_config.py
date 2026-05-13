"""Verify create_async_engine is called with pool_pre_ping=True and pool_recycle.

Error 2013 (Lost connection to MySQL server during query) occurs when the pool
hands out a connection that the server already closed after wait_timeout.
pool_pre_ping=True reconnects transparently on checkout; pool_recycle forces
replacement before the server-side timeout fires.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARIADB_DSN", "mysql+aiomysql://u:p@h:3306/db")
    monkeypatch.setenv("AI_API_AUTH_URL", "http://auth.example/token")
    monkeypatch.setenv("AI_LLM_API_J1_TOKEN", "j1-llm")
    monkeypatch.setenv("AI_EMBEDDING_API_J1_TOKEN", "j1-emb")
    monkeypatch.setenv("AI_RERANK_API_J1_TOKEN", "j1-rerank")
    monkeypatch.setenv("EMBEDDING_API_URL", "http://emb.example")
    monkeypatch.setenv("LLM_API_URL", "http://llm.example")
    monkeypatch.setenv("RERANK_API_URL", "http://rerank.example")
    monkeypatch.setenv("ES_HOSTS", "http://es.example:9200")
    monkeypatch.setenv(
        "MINIO_SITES",
        '[{"name":"__default__","endpoint":"minio.example:9000",'
        '"access_key":"ak","secret_key":"sk","bucket":"b"}]',
    )
    monkeypatch.delenv("MARIADB_POOL_RECYCLE_SECONDS", raising=False)


def test_async_engine_pool_pre_ping_and_recycle(_env: None) -> None:
    """build_container must pass pool_pre_ping=True and pool_recycle to the engine."""
    captured: dict = {}

    def fake_create_async_engine(url: str, **kwargs: object) -> MagicMock:
        captured["kwargs"] = kwargs
        mock_engine = MagicMock()
        mock_engine.url = url
        return mock_engine

    # create_async_engine is imported inside build_container(); patch the source.
    with (
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=fake_create_async_engine,
        ),
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch(
            "haystack_integrations.document_stores.elasticsearch.ElasticsearchDocumentStore",
            MagicMock(),
        ),
        patch("elasticsearch.Elasticsearch", MagicMock()),
        patch("ragent.pipelines.chat.build_retrieval_pipeline", MagicMock()),
        patch("ragent.pipelines.factory.build_ingest_pipeline", MagicMock()),
        patch("ragent.repositories.document_repository.DocumentRepository", MagicMock()),
        patch("ragent.storage.minio_registry.MinioSiteRegistry", MagicMock()),
        patch("ragent.clients.rate_limiter.RateLimiter", MagicMock()),
        patch("ragent.plugins.registry.PluginRegistry", MagicMock()),
        patch("ragent.plugins.vector.VectorExtractor", MagicMock()),
        patch("ragent.plugins.stub_graph.StubGraphExtractor", MagicMock()),
        patch("httpx.Client"),
    ):
        import ragent.bootstrap.composition as comp

        comp.build_container()

    assert captured, "create_async_engine was never called"
    assert captured["kwargs"].get("pool_pre_ping") is True, (
        "pool_pre_ping=True is required to reconnect stale connections (error 2013)"
    )
    assert "pool_recycle" in captured["kwargs"], (
        "pool_recycle is required to drop connections before server wait_timeout fires"
    )
    assert captured["kwargs"]["pool_recycle"] == 280, (
        "default pool_recycle must be 280 s (safely below 300 s server wait_timeout)"
    )


def test_async_engine_pool_recycle_env_override(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MARIADB_POOL_RECYCLE_SECONDS overrides the default pool_recycle value."""
    monkeypatch.setenv("MARIADB_POOL_RECYCLE_SECONDS", "600")
    captured: dict = {}

    def fake_create_async_engine(url: str, **kwargs: object) -> MagicMock:
        captured["kwargs"] = kwargs
        mock_engine = MagicMock()
        mock_engine.url = url
        return mock_engine

    with (
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=fake_create_async_engine,
        ),
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch(
            "haystack_integrations.document_stores.elasticsearch.ElasticsearchDocumentStore",
            MagicMock(),
        ),
        patch("elasticsearch.Elasticsearch", MagicMock()),
        patch("ragent.pipelines.chat.build_retrieval_pipeline", MagicMock()),
        patch("ragent.pipelines.factory.build_ingest_pipeline", MagicMock()),
        patch("ragent.repositories.document_repository.DocumentRepository", MagicMock()),
        patch("ragent.storage.minio_registry.MinioSiteRegistry", MagicMock()),
        patch("ragent.clients.rate_limiter.RateLimiter", MagicMock()),
        patch("ragent.plugins.registry.PluginRegistry", MagicMock()),
        patch("ragent.plugins.vector.VectorExtractor", MagicMock()),
        patch("ragent.plugins.stub_graph.StubGraphExtractor", MagicMock()),
        patch("httpx.Client"),
    ):
        import ragent.bootstrap.composition as comp

        comp.build_container()

    assert captured["kwargs"]["pool_recycle"] == 600
