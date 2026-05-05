"""Session-scoped testcontainer fixtures for integration tests (T0.9)."""

import json
import time
import urllib.request

import pytest
from haystack.dataclasses import Document


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
    _future_ms = 9_999_999_999_000
    stubs = [
        # Auth: POST /auth/api/accesstoken
        {
            "request": {"method": "POST", "urlPath": "/auth/api/accesstoken"},
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "jsonBody": {"access_token": "test-token", "expiresAt": _future_ms},
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
    return f"mysql+pymysql://ragent:ragent@{host}:{port}/ragent?charset=utf8mb4"


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
        "AUTH_URL": wiremock_url,
        "AUTH_CLIENT_ID": "ragent-test",
        "AUTH_CLIENT_SECRET": "secret",
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
