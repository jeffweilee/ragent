"""Unit tests for init_schema.py — mock DB and ES so Docker is not required."""

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from ragent.bootstrap.init_schema import (
    ESPluginMissingError,
    _es_request,
    auto_init,
    check_es_plugins,
    init_es,
    init_mariadb,
)


def _node_resp(plugins: list[str]) -> dict:
    return {
        "nodes": {
            "n1": {"plugins": [{"name": p} for p in plugins]},
        }
    }


# ── check_es_plugins ────────────────────────────────────────────────────────


def test_check_es_plugins_returns_empty_when_all_present() -> None:
    resp = _node_resp(["analysis-icu", "other-plugin"])
    with patch("ragent.bootstrap.init_schema._es_request", return_value=resp):
        assert check_es_plugins("http://es:9200") == []


def test_check_es_plugins_returns_missing_plugin() -> None:
    resp = _node_resp(["other-plugin"])
    with patch("ragent.bootstrap.init_schema._es_request", return_value=resp):
        missing = check_es_plugins("http://es:9200")
    assert "analysis-icu" in missing


def test_check_es_plugins_all_missing_when_nodes_empty() -> None:
    with patch("ragent.bootstrap.init_schema._es_request", return_value={"nodes": {}}):
        missing = check_es_plugins("http://es:9200")
    assert "analysis-icu" in missing


# ── init_es ─────────────────────────────────────────────────────────────────


def test_init_es_creates_index_when_absent() -> None:
    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if "/_nodes/plugins" in url:
            return _node_resp(["analysis-icu"])
        if method == "HEAD":
            return None  # index does not exist
        return {}  # PUT success

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")  # must not raise


def test_init_es_skips_existing_index() -> None:
    calls = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        calls.append((method, url))
        if "/_nodes/plugins" in url:
            return _node_resp(["analysis-icu"])
        if method == "HEAD":
            return {}  # index exists
        return {}

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")

    put_calls = [c for c in calls if c[0] == "PUT"]
    assert not put_calls, "PUT should not be called when index already exists"


def test_init_es_raises_plugin_missing_error() -> None:
    with (
        patch(
            "ragent.bootstrap.init_schema._es_request",
            return_value=_node_resp(["other"]),
        ),
        pytest.raises(ESPluginMissingError) as exc_info,
    ):
        init_es("http://es:9200")
    assert "analysis-icu" in exc_info.value.missing


# ── init_mariadb ─────────────────────────────────────────────────────────────


def test_init_mariadb_executes_schema_statements() -> None:
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    init_mariadb(mock_engine)
    # Should call execute at least twice (documents + chunks tables)
    assert mock_conn.execute.call_count >= 2
    mock_conn.commit.assert_called_once()


# ── _es_request ──────────────────────────────────────────────────────────────


def test_es_request_returns_parsed_json_on_success() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": True}).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("ragent.bootstrap.init_schema.urlopen", return_value=mock_resp):
        result = _es_request("http://es:9200/_cat")
    assert result == {"ok": True}


def test_es_request_returns_none_on_404() -> None:
    err = HTTPError("http://x", 404, "Not Found", {}, None)
    with patch("ragent.bootstrap.init_schema.urlopen", side_effect=err):
        result = _es_request("http://es:9200/missing", method="HEAD")
    assert result is None


def test_es_request_reraises_non_404_http_error() -> None:
    err = HTTPError("http://x", 500, "Server Error", {}, None)
    with patch("ragent.bootstrap.init_schema.urlopen", side_effect=err), pytest.raises(HTTPError):
        _es_request("http://es:9200/index")


def test_check_es_plugins_returns_all_required_when_nodes_unreachable() -> None:
    with patch("ragent.bootstrap.init_schema._es_request", return_value=None):
        missing = check_es_plugins("http://es:9200")
    assert missing == ["analysis-icu"]


# ── auto_init ────────────────────────────────────────────────────────────────


def test_auto_init_calls_init_mariadb_and_init_es() -> None:
    with (
        patch("ragent.bootstrap.init_schema.init_mariadb") as mock_db,
        patch("ragent.bootstrap.init_schema.init_es") as mock_es,
        patch("sqlalchemy.create_engine") as mock_engine_fn,
    ):
        auto_init("mysql+pymysql://u:p@h/db", "http://es:9200")
    mock_engine_fn.assert_called_once_with("mysql+pymysql://u:p@h/db")
    mock_db.assert_called_once()
    mock_es.assert_called_once_with("http://es:9200")


# ── ESPluginMissingError ─────────────────────────────────────────────────────


def test_es_plugin_missing_error_carries_missing_list() -> None:
    err = ESPluginMissingError(["analysis-icu", "other"])
    assert err.missing == ["analysis-icu", "other"]
    assert "analysis-icu" in str(err)
