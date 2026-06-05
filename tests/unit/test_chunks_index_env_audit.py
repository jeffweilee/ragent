"""T-EI.1 — `ES_CHUNKS_INDEX` audit: every production callsite must thread
the env-derived `container.chunks_index_name`, never silently fall back to
a literal `"chunks_v1"`. Defaults on callee `__init__` remain as a
test-convenience safety net; these tests pin the production wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_CUSTOM_INDEX = "chunks_audit_probe_v1"


def test_build_probes_uses_container_chunks_index_name() -> None:
    """`/readyz` must probe whatever index name composition selected (env
    `ES_CHUNKS_INDEX`), not a hardcoded `"chunks_v1"`."""
    from ragent.bootstrap.app import _build_probes

    container = SimpleNamespace(
        engine=MagicMock(),
        es_client=MagicMock(),
        minio_registry=MagicMock(),
        rate_limiter=SimpleNamespace(_redis=None),
        chunks_index_name=_CUSTOM_INDEX,
    )
    with patch("ragent.routers.health_probes.probe_es") as probe_es:
        _build_probes(container)
    probe_es.assert_called_once()
    assert probe_es.call_args.kwargs["index_names"] == [_CUSTOM_INDEX]


@pytest.fixture()
def _composition_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimum env for `build_container()` to traverse past the env-validation
    front-matter and reach the wiring sites under test."""
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
    monkeypatch.setenv("ES_CHUNKS_INDEX", _CUSTOM_INDEX)


def test_composition_threads_es_chunks_index_to_vector_extractor(
    _composition_env: None,
) -> None:
    """`VectorExtractor` must receive composition's `chunks_index_name`
    (env `ES_CHUNKS_INDEX`); falling back to the callee default `"chunks_v1"`
    is a production bug because ES writes would land on the wrong index."""
    vector_spy = MagicMock(name="VectorExtractor")

    with (
        patch("ragent.plugins.vector.VectorExtractor", vector_spy),
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping"),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock()),
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
        patch("ragent.plugins.stub_graph.StubGraphExtractor", MagicMock()),
        patch("httpx.Client"),
    ):
        import ragent.bootstrap.composition as comp

        comp.build_container()

    vector_spy.assert_called_once()
    kwargs = vector_spy.call_args.kwargs
    assert kwargs.get("index") == _CUSTOM_INDEX, (
        f"VectorExtractor received index={kwargs.get('index')!r}; "
        f"expected {_CUSTOM_INDEX!r} (env ES_CHUNKS_INDEX). composition.py "
        "must pass `index=chunks_index_name` — silently using the default "
        "`'chunks_v1'` would write to the wrong ES index in overridden envs."
    )
    assert kwargs.get("registry") is not None, (
        "VectorExtractor must receive registry=embedding_registry (B62 / issue #147) "
        "so that delete() fans out across stable + candidate indices during lifecycle migration."
    )


def test_composition_threads_es_chunks_index_to_feedback_retriever(
    _composition_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_FeedbackMemoryRetriever` must receive composition's
    `chunks_index_name`; falling back to the callee default means the
    feedback retriever's ES `terms` query would hit the wrong index when
    `ES_CHUNKS_INDEX` is overridden."""
    monkeypatch.setenv("CHAT_FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("FEEDBACK_HMAC_SECRET", "test-secret")

    feedback_spy = MagicMock(name="_FeedbackMemoryRetriever")

    with (
        patch("ragent.pipelines.retrieve._FeedbackMemoryRetriever", feedback_spy),
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping"),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock()),
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
        patch("ragent.repositories.feedback_repository.FeedbackRepository", MagicMock()),
        patch("ragent.storage.minio_registry.MinioSiteRegistry", MagicMock()),
        patch("ragent.clients.rate_limiter.RateLimiter", MagicMock()),
        patch("ragent.plugins.registry.PluginRegistry", MagicMock()),
        patch("ragent.plugins.vector.VectorExtractor", MagicMock()),
        patch("ragent.plugins.stub_graph.StubGraphExtractor", MagicMock()),
        patch("httpx.Client"),
    ):
        import ragent.bootstrap.composition as comp

        comp.build_container()

    feedback_spy.assert_called_once()
    kwargs = feedback_spy.call_args.kwargs
    expected_alias = f"{_CUSTOM_INDEX}_active"
    assert kwargs.get("chunks_index") == expected_alias, (
        f"_FeedbackMemoryRetriever received chunks_index={kwargs.get('chunks_index')!r}; "
        f"expected {expected_alias!r} (read alias of ES_CHUNKS_INDEX). composition.py "
        "must pass `chunks_index=chunks_read_alias` so feedback retrieval targets the "
        "live physical index transparently via alias."
    )
