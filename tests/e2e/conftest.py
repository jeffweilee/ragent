"""Shared E2E helpers: process spawn, API readiness, common URL."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

API_URL = "http://127.0.0.1:8000"


def _ensure_default_bucket(minio_endpoint: str) -> None:
    """Create the default upload bucket if it doesn't exist.

    The integration `minio_container` fixture spins up a fresh MinIO
    server with no buckets. Code paths that POST /ingest expect the
    bucket from MINIO_BUCKET (defaults to "ragent-uploads") to already
    exist — without this, every e2e ingest 500s on NoSuchBucket.
    """
    from minio import Minio

    bucket = os.environ.get("MINIO_BUCKET", "ragent-uploads")
    client = Minio(
        minio_endpoint,
        access_key="minioadmin",
        secret_key="minioadmin",
        secure=False,
    )
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def _purge_state(mariadb_dsn: str, es_url: str) -> None:
    """Wipe MariaDB rows + ES docs that accumulate across e2e tests.

    Prerequisite for letting multiple e2e tests share a session-scoped
    api/worker subprocess (B). Without this, doc_id collisions and stale
    chunk hits make later tests non-deterministic.
    """
    import urllib.request
    from sqlalchemy import create_engine, text

    sync_dsn = mariadb_dsn.replace("mysql+aiomysql://", "mysql+pymysql://")
    engine = create_engine(sync_dsn)
    with engine.begin() as conn:
        for table in ("documents",):
            conn.execute(text(f"DELETE FROM {table}"))
    engine.dispose()

    body = b'{"query": {"match_all": {}}}'
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_delete_by_query?refresh=true&conflicts=proceed",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


@pytest.fixture
def e2e_env(
    dev_env,
    minio_endpoint: str,
    mariadb_dsn: str,
    es_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """E2E env layered on the integration `dev_env` fixture.

    Purges MariaDB + ES on entry so each test starts from a clean slate
    even when a session-scoped api/worker keeps writing to the same DB.
    """
    monkeypatch.setenv("RAGENT_PORT", "8000")
    _ensure_default_bucket(minio_endpoint)
    _purge_state(mariadb_dsn, es_url)
    yield


def _build_dev_env(
    *,
    mariadb_dsn: str,
    es_url: str,
    minio_endpoint: str,
    redis_url: str,
    wiremock_url: str,
) -> dict[str, str]:
    """Same env table as the function-scope `dev_env` fixture, but as a
    plain dict so a session-scope subprocess can inherit it via
    ``os.environ.update`` (monkeypatch is function-scope and would
    unset values between tests, breaking long-lived child processes).
    """
    return {
        "RAGENT_ENV": "dev",
        "RAGENT_AUTH_DISABLED": "true",
        "RAGENT_HOST": "127.0.0.1",
        "RAGENT_PORT": "8000",
        "AI_API_AUTH_URL": wiremock_url,
        "AI_LLM_API_J1_TOKEN": "test-llm-j1",
        "AI_EMBEDDING_API_J1_TOKEN": "test-embedding-j1",
        "AI_RERANK_API_J1_TOKEN": "test-rerank-j1",
        "EMBEDDING_API_URL": wiremock_url,
        "LLM_API_URL": wiremock_url,
        "RERANK_API_URL": f"{wiremock_url}/rerank",
        "EMBEDDER_BATCH_SIZE": "1",
        "MINIO_ENDPOINT": minio_endpoint,
        "MINIO_ACCESS_KEY": "minioadmin",
        "MINIO_SECRET_KEY": "minioadmin",
        "ES_HOSTS": es_url,
        "ES_VERIFY_CERTS": "false",
        "MARIADB_DSN": mariadb_dsn,
        "REDIS_BROKER_URL": f"{redis_url}/0",
        "REDIS_RATELIMIT_URL": f"{redis_url}/1",
    }


@pytest.fixture(scope="session")
def running_stack(
    mariadb_dsn: str,
    es_url: str,
    minio_endpoint: str,
    redis_url: str,
    wiremock_url: str,
) -> Iterator[None]:
    """Spawn one api + one worker and reuse them across the whole e2e session.

    Tests share a single subprocess pair; per-test isolation comes from
    `e2e_env` purging MariaDB rows + ES docs on entry. Chaos tests that
    deliberately kill the worker keep using their own function-scope
    `spawn_module` and skip this fixture.
    """
    os.environ.update(
        _build_dev_env(
            mariadb_dsn=mariadb_dsn,
            es_url=es_url,
            minio_endpoint=minio_endpoint,
            redis_url=redis_url,
            wiremock_url=wiremock_url,
        )
    )
    _ensure_default_bucket(minio_endpoint)

    procs: list[subprocess.Popen] = []
    for module in ("ragent.api", "ragent.worker"):
        log_path = f"/tmp/e2e_{module.replace('.', '_')}.log"
        out = open(log_path, "w")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            env={**os.environ},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)

    wait_api_ready(timeout=45)
    yield

    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


@pytest.fixture
def spawn_module() -> Iterator[callable]:
    """Spawn `python -m <module>` subprocesses; auto-terminate on test exit."""
    procs: list[subprocess.Popen] = []

    def _spawn(module: str) -> subprocess.Popen:
        log_path = f"/tmp/e2e_{module.replace('.', '_')}.log"
        out = open(log_path, "w")  # noqa: SIM115 — fd lifetime tied to procs list
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            env={**os.environ},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    yield _spawn

    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def wait_api_ready(timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{API_URL}/livez", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("API never reached /livez=200")
