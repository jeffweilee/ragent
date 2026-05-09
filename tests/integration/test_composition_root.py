"""T7.5b — Composition root: get_container() builds a fully-wired graph (B17, B30)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.docker


def test_get_container_builds_fully_wired_graph(dev_env) -> None:
    from ragent.bootstrap.composition import get_container

    container = get_container()
    assert container.doc_repo is not None
    assert container.minio_registry is not None
    assert container.es_client is not None
    assert container.engine is not None
    assert container.registry is not None


def test_vector_extractor_receives_same_dependency_instances(dev_env) -> None:
    """B17 DI verified by identity."""
    from ragent.bootstrap.composition import get_container

    container = get_container()
    vector = container.registry._plugins.get("vector")  # noqa: SLF001
    assert vector is not None
    assert vector._repo is container.doc_repo  # noqa: SLF001
    assert vector._embedder is container.embedding_client  # noqa: SLF001
    assert vector._es is container.es_client  # noqa: SLF001


def test_missing_required_env_var_exits_eagerly(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit at construction, not lazy at first request (B30 fail-fast)."""
    for var in ("AI_API_AUTH_URL", "MARIADB_DSN", "EMBEDDING_API_URL"):
        monkeypatch.delenv(var, raising=False)
    import ragent.bootstrap.composition as comp

    comp._container = None  # noqa: SLF001
    with pytest.raises(SystemExit):
        comp.get_container()


def test_retrieval_pipeline_is_runnable(dev_env) -> None:
    from ragent.bootstrap.composition import get_container

    container = get_container()
    assert hasattr(container.retrieval_pipeline, "run") or callable(container.retrieval_pipeline)
