"""Session-scoped testcontainer fixtures for integration tests (T0.9)."""

import pytest

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
    """ES with analysis-icu plugin if buildable, else plain ES 9.2.3."""
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available")
    import subprocess
    from pathlib import Path

    from testcontainers.elasticsearch import ElasticSearchContainer

    dockerfile = Path(__file__).parents[1] / "Dockerfile.es-test"
    image = "elasticsearch:9.2.3"
    icu_built = False
    if dockerfile.exists():
        build = subprocess.run(
            ["docker", "build", "-f", str(dockerfile), "-t", "ragent-es-test:latest", "."],
            cwd=str(Path(__file__).parents[1]),
            capture_output=True,
        )
        if build.returncode == 0:
            image = "ragent-es-test:latest"
            check = subprocess.run(
                ["docker", "run", "--rm", "ragent-es-test:latest",
                 "bin/elasticsearch-plugin", "list"],
                capture_output=True, text=True,
            )
            icu_built = "analysis-icu" in check.stdout

    with ElasticSearchContainer(image=image, port=9200) as container:
        container.icu_available = icu_built
        yield container


@pytest.fixture(scope="session")
def es_url(es_container) -> str:
    host = es_container.get_container_host_ip()
    port = es_container.get_exposed_port(9200)
    return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def icu_available(es_container) -> bool:
    return getattr(es_container, "icu_available", False)


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
