"""T7.7 — /readyz against real testcontainers: success when up, 503 when dep down (B4)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.docker


def _build_app_with_real_probes(engine, es_client, minio_client, redis_client) -> FastAPI:
    from ragent.routers.health import create_health_router
    from ragent.routers.health_probes import (
        probe_es,
        probe_mariadb,
        probe_minio,
        probe_redis,
    )

    probes = {
        "mariadb": probe_mariadb(engine),
        "es": probe_es(es_client, index_names=[]),  # no required index in this test
        "minio": probe_minio(minio_client),
        "redis": probe_redis(redis_client),
    }
    app = FastAPI()
    app.include_router(create_health_router(probes=probes))
    return app


def test_readyz_200_when_all_deps_up(
    mariadb_dsn: str, es_url: str, minio_container, redis_container
) -> None:
    from elasticsearch import Elasticsearch
    from minio import Minio
    from redis import Redis
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(mariadb_dsn)
    es_client = Elasticsearch(hosts=[es_url], verify_certs=False)
    redis_host = redis_container.get_container_host_ip()
    redis_port = redis_container.get_exposed_port(6379)
    redis_client = Redis(host=redis_host, port=int(redis_port))

    minio_host = minio_container.get_container_host_ip()
    minio_port = minio_container.get_exposed_port(9000)
    raw_minio = Minio(
        endpoint=f"{minio_host}:{minio_port}",
        access_key="minioadmin",
        secret_key="minioadmin",
        secure=False,
    )

    app = _build_app_with_real_probes(engine, es_client, raw_minio, redis_client)
    with TestClient(app) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_readyz_503_when_mariadb_down() -> None:
    """A bogus engine URL → MariaDB probe fails → 503 with DEPENDENCY_DOWN."""
    from sqlalchemy import create_engine

    from ragent.routers.health import create_health_router
    from ragent.routers.health_probes import probe_mariadb

    bad_engine = create_engine("mysql+pymysql://nobody:wrong@127.0.0.1:1/none")
    probes = {"mariadb": probe_mariadb(bad_engine)}
    app = FastAPI()
    app.include_router(create_health_router(probes=probes))
    with TestClient(app) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] in ("DEPENDENCY_DOWN", "PROBE_TIMEOUT")


def test_metrics_endpoint_exposes_required_counters() -> None:
    """T7.7 spec: /metrics exposes reconciler_tick_total, worker_pipeline_duration_seconds,
    minio_orphan_object_total, multi_ready_repaired_total."""
    from ragent.bootstrap.metrics import setup_metrics
    from ragent.routers.health import create_health_router

    app = FastAPI()
    app.include_router(create_health_router())
    setup_metrics(app)
    with TestClient(app) as client:
        body = client.get("/metrics").text
        for name in (
            "reconciler_tick_total",
            "worker_pipeline_duration_seconds",
            "minio_orphan_object_total",
            "multi_ready_repaired_total",
        ):
            assert name in body, f"metrics endpoint missing {name}"
