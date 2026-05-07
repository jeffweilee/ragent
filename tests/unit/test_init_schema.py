"""Unit tests for init_schema.py — mock DB and ES so Docker is not required."""

import base64
import json
import os
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from ragent.bootstrap.init_schema import (
    _es_auth_headers,
    _es_request,
    auto_init,
    init_es,
    init_mariadb,
)

# ── init_es ─────────────────────────────────────────────────────────────────


def test_init_es_creates_index_when_absent() -> None:
    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "HEAD":
            return None  # index does not exist
        return {}  # PUT success

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")  # must not raise


def test_init_es_skips_existing_index() -> None:
    calls = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        calls.append((method, url))
        if method == "HEAD":
            return {}  # index exists
        return {}

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")

    put_calls = [c for c in calls if c[0] == "PUT"]
    assert not put_calls, "PUT should not be called when index already exists"


# ── init_mariadb ─────────────────────────────────────────────────────────────


def test_init_mariadb_executes_schema_statements() -> None:
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_conn
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    init_mariadb(mock_engine)
    # documents table is the only DDL statement after C6 (chunks dropped).
    assert mock_conn.execute.call_count >= 1


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


def test_es_request_returns_empty_dict_for_head_with_no_body() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("ragent.bootstrap.init_schema.urlopen", return_value=mock_resp):
        result = _es_request("http://es:9200/chunks_v1", method="HEAD")
    assert result == {}


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


# ── ES auth headers ──────────────────────────────────────────────────────────


def test_es_auth_headers_uses_api_key_when_set() -> None:
    with patch.dict(os.environ, {"ES_API_KEY": "my-key"}, clear=False):
        headers = _es_auth_headers()
    assert headers == {"Authorization": "ApiKey my-key"}


def test_es_auth_headers_uses_basic_auth_when_no_api_key() -> None:
    env = {"ES_USERNAME": "user", "ES_PASSWORD": "pass"}
    with patch.dict(os.environ, env, clear=False):
        headers = _es_auth_headers()
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert headers == {"Authorization": expected}


def test_es_auth_headers_api_key_takes_precedence_over_basic() -> None:
    env = {"ES_API_KEY": "key", "ES_USERNAME": "u", "ES_PASSWORD": "p"}
    with patch.dict(os.environ, env, clear=False):
        headers = _es_auth_headers()
    assert headers["Authorization"].startswith("ApiKey ")


def test_es_auth_headers_empty_when_no_credentials() -> None:
    with patch.dict(os.environ, {}, clear=True):
        headers = _es_auth_headers()
    assert headers == {}
