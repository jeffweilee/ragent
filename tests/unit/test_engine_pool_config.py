"""Verify create_async_engine is called with pool_pre_ping=True and pool_recycle,
and that the aiomysql ping signature workaround is applied.

Error 2013 (Lost connection to MySQL server during query) occurs when the pool
hands out a connection that the server already closed after wait_timeout.
pool_pre_ping=True reconnects transparently on checkout; pool_recycle forces
replacement before the server-side timeout fires.

The aiomysql adapter declares ping(self, reconnect: bool) with no default.
SQLAlchemy's do_ping calls ping() with no args when pymysql's reconnect param
is absent or defaults to False (_send_false_to_ping=False path). _wrap_ping
patches each connection instance to add reconnect=False so both call sites work.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # non-JWT auth mode so build_container doesn't require OIDC_*
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")
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
        (
            '[{"name":"__default__","endpoint":"minio.example:9000",'
            '"access_key":"ak","secret_key":"example_minio_secret_not_real",'
            '"bucket":"b"}]'
        ),  # pragma: allowlist secret
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
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping") as mock_patch_ping,
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch(
            "haystack_integrations.document_stores.elasticsearch.ElasticsearchDocumentStore",
            MagicMock(),
        ),
        patch("elasticsearch.Elasticsearch", MagicMock()),
        patch("ragent.pipelines.retrieve.build_retrieval_pipeline", MagicMock()),
        patch("ragent.pipelines.ingest.build_ingest_pipeline", MagicMock()),
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
    mock_patch_ping.assert_called_once()


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
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping"),
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch(
            "haystack_integrations.document_stores.elasticsearch.ElasticsearchDocumentStore",
            MagicMock(),
        ),
        patch("elasticsearch.Elasticsearch", MagicMock()),
        patch("ragent.pipelines.retrieve.build_retrieval_pipeline", MagicMock()),
        patch("ragent.pipelines.ingest.build_ingest_pipeline", MagicMock()),
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


def test_wrap_ping_adds_default_reconnect() -> None:
    """_wrap_ping patches ping() to accept no-arg call, defaulting reconnect=False."""
    from ragent.bootstrap.init_schema import _wrap_ping

    calls: list[bool] = []

    class _FakeConn:
        def ping(self, reconnect: bool) -> None:
            calls.append(reconnect)

    conn = _FakeConn()
    _wrap_ping(conn)

    conn.ping()  # must not raise TypeError
    assert calls == [False], "default reconnect must be False"

    conn.ping(True)
    assert calls == [False, True], "explicit arg must be forwarded unchanged"


def test_reconciler_engine_pool_pre_ping_and_recycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconciler._tick builds engine with pool_pre_ping=True and pool_recycle."""
    from unittest.mock import AsyncMock

    import ragent.reconciler as rec_mod

    monkeypatch.setenv("MARIADB_DSN", "mysql+aiomysql://x:y@h/db")

    captured: dict = {}

    def _fake_engine(dsn: str, **kwargs: object) -> MagicMock:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        m = MagicMock()
        m.sync_engine = MagicMock()
        m.dispose = AsyncMock()
        return m

    fake_broker = MagicMock()
    fake_broker.startup = AsyncMock()
    fake_broker.shutdown = AsyncMock()

    stub_repo = AsyncMock()
    stub_repo.list_pending_stale.return_value = []
    stub_repo.list_pending_exceeded.return_value = []
    stub_repo.list_uploaded_stale.return_value = []
    stub_repo.list_deleting_stale.return_value = []
    stub_repo.find_multi_ready_groups.return_value = []

    import ragent.workers.ingest  # noqa: F401 — register @broker.task before patch

    ping_patch_calls: list = []
    monkeypatch.setattr(rec_mod, "create_async_engine", _fake_engine)
    monkeypatch.setattr(rec_mod, "DocumentRepository", lambda engine: stub_repo)
    monkeypatch.setattr("ragent.bootstrap.broker.broker", fake_broker)
    monkeypatch.setattr(
        "ragent.bootstrap.composition.get_container",
        lambda: MagicMock(
            registry=MagicMock(),
            embedding_registry=MagicMock(refresh=AsyncMock()),
        ),
    )
    monkeypatch.setattr(
        "ragent.bootstrap.init_schema.patch_aiomysql_ping",
        lambda engine: ping_patch_calls.append(engine),
    )

    runner = rec_mod._build_from_env()
    runner.run()

    assert captured.get("kwargs", {}).get("pool_pre_ping") is True, (
        "reconciler engine must use pool_pre_ping=True"
    )
    assert "pool_recycle" in captured.get("kwargs", {}), "reconciler engine must set pool_recycle"
    assert len(ping_patch_calls) == 1, "patch_aiomysql_ping must be called on the reconciler engine"
