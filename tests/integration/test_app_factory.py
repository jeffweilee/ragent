"""T7.5f — create_app() factory: lifespan, routers, X-User-Id middleware (B30, C9)."""

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


def test_create_app_boots_against_real_dependencies(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """create_app() returns a FastAPI instance with all routers mounted."""
    from fastapi.testclient import TestClient

    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200


def test_livez_reachable_without_x_user_id(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """C9 — health endpoints bypass the X-User-Id middleware."""
    from fastapi.testclient import TestClient

    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/livez", headers={}).status_code == 200
        assert client.get("/metrics", headers={}).status_code == 200


def test_protected_route_returns_422_without_user_id(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """Protected routes return 422 problem+json when X-User-Id is missing."""
    from fastapi.testclient import TestClient

    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/ingest/SOME_ID")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_lifespan_runs_init_schema_once(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """init_schema fires exactly once during lifespan startup."""
    from fastapi.testclient import TestClient

    _set_required_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    _reset_container()
    calls: list[None] = []

    import ragent.bootstrap.app as app_mod

    real = app_mod.init_schema
    monkeypatch.setattr(app_mod, "init_schema", lambda: (calls.append(None), real())[1])

    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        client.get("/livez")
    assert len(calls) == 1
