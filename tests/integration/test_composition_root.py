"""T7.5b — Composition root: get_container() builds a fully-wired graph (B17, B30)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.docker


def _set_required_env(monkeypatch, mariadb_dsn: str, es_url: str, minio_endpoint: str) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("AUTH_URL", "http://localhost:9999/oauth/token")
    monkeypatch.setenv("AUTH_CLIENT_ID", "ragent-test")
    monkeypatch.setenv("AUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EMBEDDING_API_URL", "http://localhost:9999/embed")
    monkeypatch.setenv("LLM_API_URL", "http://localhost:9999/chat")
    monkeypatch.setenv("RERANK_API_URL", "http://localhost:9999/rerank")
    monkeypatch.setenv("MINIO_ENDPOINT", minio_endpoint)
    monkeypatch.setenv("MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minioadmin")
    monkeypatch.setenv("ES_HOSTS", es_url)
    monkeypatch.setenv("ES_VERIFY_CERTS", "false")
    monkeypatch.setenv("MARIADB_DSN", mariadb_dsn)
    monkeypatch.setenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("REDIS_RATELIMIT_URL", "redis://localhost:6379/1")


def _reset_container() -> None:
    import ragent.bootstrap.composition as comp

    comp._container = None  # noqa: SLF001


def test_get_container_builds_fully_wired_graph(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """container exposes all DI dependencies in one resolved graph."""
    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.composition import get_container

    container = get_container()
    assert container.doc_repo is not None
    assert container.chunk_repo is not None
    assert container.minio_client is not None
    assert container.es_client is not None
    assert container.engine is not None
    assert container.registry is not None


def test_vector_extractor_receives_same_dependency_instances(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """B17 DI verified by identity: VectorExtractor.__init__ uses the container's
    repo/chunks/embedder/es."""
    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.composition import get_container

    container = get_container()
    vector = container.registry._plugins.get("vector")  # noqa: SLF001
    assert vector is not None
    assert vector._repo is container.doc_repo  # noqa: SLF001
    assert vector._chunks is container.chunk_repo  # noqa: SLF001
    assert vector._embedder is container.embedding_client  # noqa: SLF001
    assert vector._es is container.es_client  # noqa: SLF001


def test_missing_required_env_var_exits_eagerly(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit at construction, not lazy at first request (B30 fail-fast)."""
    for var in ("AUTH_URL", "MARIADB_DSN", "EMBEDDING_API_URL"):
        monkeypatch.delenv(var, raising=False)
    _reset_container()
    from ragent.bootstrap.composition import get_container

    with pytest.raises(SystemExit):
        get_container()


def test_retrieval_pipeline_is_runnable(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """container.retrieval_pipeline returns a Haystack Pipeline (callable .run)."""
    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.composition import get_container

    container = get_container()
    assert hasattr(container.retrieval_pipeline, "run") or callable(container.retrieval_pipeline)
