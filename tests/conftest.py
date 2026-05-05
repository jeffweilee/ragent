"""Session-scoped testcontainer fixtures for integration tests (T0.9)."""

import json
import time
import urllib.request

import pytest


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
