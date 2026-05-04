"""T0.8g — ES without analysis-icu plugin → ESPluginMissingError; index not created."""

import pytest

from ragent.bootstrap.init_schema import ESPluginMissingError, check_es_plugins, init_es

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def es_no_icu():
    """Bare ES container without analysis-icu plugin."""
    from testcontainers.elasticsearch import ElasticSearchContainer

    with ElasticSearchContainer(image="elasticsearch:9.2.3", port=9200) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(9200)
        yield f"http://{host}:{port}"


def test_check_es_plugins_reports_missing_icu(es_no_icu: str) -> None:
    missing = check_es_plugins(es_no_icu)
    assert "analysis-icu" in missing


def test_init_es_raises_when_plugin_missing(es_no_icu: str) -> None:
    with pytest.raises(ESPluginMissingError) as exc_info:
        init_es(es_no_icu)
    assert "analysis-icu" in exc_info.value.missing


def test_index_not_created_when_plugin_missing(es_no_icu: str) -> None:
    import contextlib

    with contextlib.suppress(ESPluginMissingError):
        init_es(es_no_icu)
    from ragent.bootstrap.init_schema import _es_request

    result = _es_request(f"{es_no_icu}/chunks_v1")
    assert result is None, "chunks_v1 index must not be created when analysis-icu is missing"
