"""Session-scoped testcontainer fixtures for integration tests (T0.9)."""

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from haystack.dataclasses import Document


def run_in_threadpool(fn: Callable[[], Any]) -> Any:
    """Run a sync callable inside ``anyio.to_thread.run_sync``.

    Pipeline components like ``_SourceHydrator`` and ``_IdempotencyClean`` use
    ``anyio.from_thread.run`` to bridge sync→async, which only works when the
    caller is on an anyio worker thread. Tests that invoke the pipeline directly
    (no FastAPI ``run_in_threadpool`` wrapper) must establish the bridge here.
    """

    async def _wrap() -> Any:
        return await anyio.to_thread.run_sync(fn)

    return anyio.run(_wrap)


def make_ingest_container(doc: Any, *, pipeline_side_effect: Any = None) -> MagicMock:
    """Mock composition container used by ``ingest_pipeline_task`` tests."""
    container = MagicMock()
    container.doc_repo = AsyncMock()
    container.doc_repo.claim_for_processing.return_value = doc
    container.minio_client = MagicMock()
    container.minio_client.get_object.return_value = b"data"
    if pipeline_side_effect is not None:
        container.ingest_pipeline.run.side_effect = pipeline_side_effect
    else:
        container.ingest_pipeline.run.return_value = {"writer": {"documents_written": []}}
    container.registry = AsyncMock()
    return container


class FakeDocumentStore:
    """In-memory DocumentStore stand-in used by ingest pipeline tests."""

    def __init__(self) -> None:
        self.written: list[Document] = []

    def write_documents(self, documents: list[Document], policy=None) -> int:  # noqa: ANN001
        self.written.extend(documents)
        return len(documents)

    def count_documents(self) -> int:
        return len(self.written)

    def filter_documents(self, filters=None) -> list[Document]:  # noqa: ANN001
        return list(self.written)


def _wait_es_yellow(url: str, timeout: int = 120) -> None:
    """Block until ES cluster health is at least yellow (shards allocated)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{url}/_cluster/health?wait_for_status=yellow&timeout=10s", timeout=15
            ) as resp:
                health = json.loads(resp.read())
                if health.get("status") in ("yellow", "green"):
                    return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"ES at {url} did not reach yellow status within {timeout}s")


def _wait_wiremock_ready(url: str, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/__admin/health", timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"WireMock at {url} did not become ready within {timeout}s")


def _configure_wiremock_stubs(base_url: str) -> None:
    """Register default stubs for all external API endpoints."""
    # Expiry far in the future so the token is never refreshed during tests.
    _future_iso = "2999-01-01T00:00:00Z"
    stubs = [
        # Auth: POST /auth/api/accesstoken — {"key": j1} → {"token": j2, "expiresAt": ISO}
        {
            "request": {"method": "POST", "urlPath": "/auth/api/accesstoken"},
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "jsonBody": {"token": "test-j2-token", "expiresAt": _future_iso},
            },
        },
        # Embedding: POST /text_embedding — returns one 1024-dim zero vector.
        # Set EMBEDDER_BATCH_SIZE=1 in dev_env so each request sends exactly one
        # text and the fixed single-vector response stays consistent.
        {
            "request": {"method": "POST", "urlPath": "/text_embedding"},
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "jsonBody": {
                    "returnCode": 96200,
                    "data": [{"embedding": [0.0] * 1024}],
                },
            },
        },
        # LLM non-streaming: body contains "stream": false
        {
            "request": {
                "method": "POST",
                "urlPath": "/gpt_oss_120b/v1/chat/completions",
                "bodyPatterns": [{"matchesJsonPath": "$[?(@.stream == false)]"}],
            },
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "jsonBody": {
                    "choices": [{"message": {"content": "test response"}}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            },
        },
        # LLM streaming: body contains "stream": true — respond with SSE
        {
            "request": {
                "method": "POST",
                "urlPath": "/gpt_oss_120b/v1/chat/completions",
                "bodyPatterns": [{"matchesJsonPath": "$[?(@.stream == true)]"}],
            },
            "response": {
                "status": 200,
                "headers": {"Content-Type": "text/event-stream"},
                "body": ('data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'),
            },
        },
        # Rerank: POST /rerank
        {
            "request": {"method": "POST", "urlPath": "/rerank"},
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "jsonBody": {"results": [{"index": 0, "score": 0.9}]},
            },
        },
    ]
    for stub in stubs:
        data = json.dumps(stub).encode()
        req = urllib.request.Request(
            f"{base_url}/__admin/mappings",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as _:
            pass


try:
    import docker

    docker.from_env()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False


@pytest.fixture(autouse=True, scope="session")
def _ragent_logging_configured():
    """Haystack 2.x import side effects replace structlog's default processor
    chain, which breaks ``structlog.testing.capture_logs`` for already-bound
    proxy loggers. Configure once per session to restore correlation."""
    from ragent.bootstrap.logging_config import configure_logging

    configure_logging("ragent-test")
    yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "docker: mark test as requiring Docker (skipped if unavailable)"
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if DOCKER_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="Docker daemon not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def mariadb_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(
        image="mariadb:10.6",
        username="ragent",
        password="ragent",
        dbname="ragent",
    ) as container:
        yield container


@pytest.fixture(scope="session")
def mariadb_dsn(mariadb_container) -> str:
    host = mariadb_container.get_container_host_ip()
    port = mariadb_container.get_exposed_port(3306)
    return f"mysql+aiomysql://ragent:ragent@{host}:{port}/ragent?charset=utf8mb4"


@pytest.fixture(scope="session")
def es_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    from testcontainers.elasticsearch import ElasticSearchContainer

    container = ElasticSearchContainer(image="elasticsearch:9.2.3", port=9200)
    # single-node: skip cluster discovery / master election (faster startup).
    container.with_env("discovery.type", "single-node")
    # Disable disk watermark so shards allocate even on > 90%-full CI/dev VMs.
    container.with_env("cluster.routing.allocation.disk.threshold_enabled", "false")
    with container as c:
        yield c


@pytest.fixture(scope="session")
def es_url(es_container) -> str:
    host = es_container.get_container_host_ip()
    port = es_container.get_exposed_port(9200)
    url = f"http://{host}:{port}"
    _wait_es_yellow(url)
    return url


@pytest.fixture(scope="session")
def redis_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    from testcontainers.redis import RedisContainer

    with RedisContainer(image="redis:7") as container:
        yield container


@pytest.fixture(scope="session")
def minio_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    from testcontainers.minio import MinioContainer

    with MinioContainer() as container:
        yield container


@pytest.fixture(scope="session")
def minio_endpoint(minio_container) -> str:
    host = minio_container.get_container_host_ip()
    port = minio_container.get_exposed_port(9000)
    return f"{host}:{port}"


@pytest.fixture(scope="session")
def redis_url(redis_container) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}"


@pytest.fixture(scope="session")
def wiremock_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    from testcontainers.core.container import DockerContainer

    container = DockerContainer("wiremock/wiremock:latest")
    container.with_exposed_ports(8080)
    with container as c:
        yield c


@pytest.fixture(scope="session")
def wiremock_url(wiremock_container) -> str:
    host = wiremock_container.get_container_host_ip()
    port = wiremock_container.get_exposed_port(8080)
    url = f"http://{host}:{port}"
    _wait_wiremock_ready(url)
    _configure_wiremock_stubs(url)
    return url


@pytest.fixture
def dev_env(
    monkeypatch: pytest.MonkeyPatch,
    mariadb_dsn: str,
    es_url: str,
    minio_endpoint: str,
    redis_url: str,
    wiremock_url: str,
) -> None:
    """Apply RAGENT dev-mode env wired to the testcontainer fixtures (B30)."""
    pairs = {
        "RAGENT_ENV": "dev",
        "RAGENT_AUTH_DISABLED": "true",
        "RAGENT_HOST": "127.0.0.1",
        "AI_API_AUTH_URL": wiremock_url,
        "AI_LLM_API_J1_TOKEN": "test-llm-j1",
        "AI_EMBEDDING_API_J1_TOKEN": "test-embedding-j1",
        "AI_RERANK_API_J1_TOKEN": "test-rerank-j1",
        "EMBEDDING_API_URL": wiremock_url,
        "LLM_API_URL": wiremock_url,
        "RERANK_API_URL": f"{wiremock_url}/rerank",
        # 1 text per batch → the single-embedding WireMock stub stays consistent.
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
    for key, val in pairs.items():
        monkeypatch.setenv(key, val)
    import ragent.bootstrap.composition as comp

    comp._container = None  # noqa: SLF001 — composition root caches singleton
