"""T-HTTPLOG.1 — pin `install_error_logging` contract on shared httpx.Client.

`install_error_logging` wraps `httpx.Client.send` so every HTTP error
(response status >= 400 OR `httpx.HTTPError` subclass: timeout, connect,
read) emits a single structured `http.upstream_error` log record containing
the request body, response body (when available), redacted headers, status,
and exception type. Authorisation headers and the J1 `key` field are
redacted at source; bodies are truncated at `HTTP_ERROR_LOG_MAX_BYTES`.
"""

from __future__ import annotations

import json

import httpx
import pytest
import structlog


def _client(handler) -> httpx.Client:
    from ragent.bootstrap.http_logging import install_error_logging

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://upstream.test")
    install_error_logging(client, client_name="upstream")
    return client


def _auth_client(handler) -> httpx.Client:
    from ragent.bootstrap.http_logging import install_error_logging

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://auth.test")
    install_error_logging(client, client_name="auth", redact_auth_body=True)
    return client


def _find_error_log(logs: list[dict]) -> dict | None:
    for record in logs:
        if record.get("event") == "http.upstream_error":
            return record
    return None


def test_success_response_emits_no_log() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        resp = client.post("/embed", json={"texts": ["hi"]})
    assert resp.status_code == 200
    assert _find_error_log(logs) is None


def test_4xx_response_logs_request_and_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/embed", json={"texts": ["hi"]})
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["status"] == 404
    assert rec["client_name"] == "upstream"
    assert rec["method"] == "POST"
    assert "/embed" in rec["url"]
    assert '"texts"' in rec["http_request_payload"]
    assert '"not found"' in rec["http_response_payload"]
    assert rec["request_truncated"] is False
    assert rec["response_truncated"] is False


def test_5xx_response_logs_request_and_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream exploded")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/llm", json={"messages": []})
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["status"] == 500
    assert "upstream exploded" in rec["http_response_payload"]


def test_timeout_exception_logs_request_body_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _client(handler)
    with structlog.testing.capture_logs() as logs, pytest.raises(httpx.ReadTimeout):
        client.post("/embed", json={"texts": ["x"]})
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["status"] is None
    assert rec["exception_type"] == "ReadTimeout"
    assert '"texts"' in rec["http_request_payload"]
    assert "http_response_payload" not in rec


def test_connect_error_logs_request_body_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot connect", request=request)

    client = _client(handler)
    with structlog.testing.capture_logs() as logs, pytest.raises(httpx.ConnectError):
        client.post("/llm", json={"messages": [{"role": "user"}]})
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["exception_type"] == "ConnectError"
    assert '"messages"' in rec["http_request_payload"]
    assert "http_response_payload" not in rec


def test_streaming_response_error_logs_without_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"streamed error body")

    client = _client(handler)
    with (
        structlog.testing.capture_logs() as logs,
        client.stream("POST", "/llm", json={"stream": True}) as resp,
    ):
        assert resp.status_code == 500
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["status"] == 500
    assert '"stream"' in rec["http_request_payload"]
    assert "http_response_payload" not in rec


def test_auth_body_key_field_is_redacted() -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(401, json={"error": "bad key"})

    client = _auth_client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/auth", json={"key": "j1-secret-token", "other": "v"})
    rec = _find_error_log(logs)
    assert rec is not None
    payload = json.loads(rec["http_request_payload"])
    assert payload["key"] == "***"
    assert payload["other"] == "v"
    assert b"j1-secret-token" in captured["body"]


def test_authorization_header_is_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post(
            "/embed",
            json={"texts": ["x"]},
            headers={"Authorization": "Bearer secret-j2"},
        )
    rec = _find_error_log(logs)
    assert rec is not None
    headers = {k.lower(): v for k, v in rec["headers"].items()}
    assert headers.get("authorization") == "***"
    assert "secret-j2" not in json.dumps(rec["headers"])


def test_apikey_and_cookie_headers_are_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post(
            "/unprotect",
            content=b"file-bytes",
            headers={"apikey": "secret-apikey", "Cookie": "session=abc"},
        )
    rec = _find_error_log(logs)
    assert rec is not None
    headers = {k.lower(): v for k, v in rec["headers"].items()}
    assert headers["apikey"] == "***"
    assert headers["cookie"] == "***"
    assert "secret-apikey" not in json.dumps(rec["headers"])
    assert "session=abc" not in json.dumps(rec["headers"])


def test_body_over_8kb_is_truncated_with_flag() -> None:
    big = b"A" * 20000

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=big)

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/embed", content=big)
    rec = _find_error_log(logs)
    assert rec is not None
    assert len(rec["http_request_payload"]) == 8192
    assert len(rec["http_response_payload"]) == 8192
    assert rec["request_truncated"] is True
    assert rec["response_truncated"] is True


def test_non_utf8_body_decoded_with_replacement() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"\xff\xfe\x00bad")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/embed", content=b"\xff\xferaw")
    rec = _find_error_log(logs)
    assert rec is not None
    assert "�" in rec["http_request_payload"]
    assert "�" in rec["http_response_payload"]


def test_max_bytes_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_ERROR_LOG_MAX_BYTES", "128")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"X" * 300)

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/embed", content=b"Y" * 300)
    rec = _find_error_log(logs)
    assert rec is not None
    assert len(rec["http_response_payload"]) == 128
    assert len(rec["http_request_payload"]) == 128
    assert rec["response_truncated"] is True
    assert rec["request_truncated"] is True


def test_exception_is_reraised_after_logging() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    client = _client(handler)
    with pytest.raises(httpx.ConnectTimeout):
        client.post("/embed", json={"texts": ["x"]})


def test_install_marks_client_with_attribute() -> None:
    from ragent.bootstrap.http_logging import install_error_logging

    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    client = httpx.Client(transport=transport)
    install_error_logging(client, client_name="upstream")
    assert getattr(client, "__ragent_http_error_logging__", False) is True


def test_double_install_does_not_double_wrap() -> None:
    from ragent.bootstrap.http_logging import install_error_logging

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://upstream.test")
    install_error_logging(client, client_name="upstream")
    install_error_logging(client, client_name="upstream")
    with structlog.testing.capture_logs() as logs:
        client.post("/embed", json={"texts": ["x"]})
    error_logs = [r for r in logs if r.get("event") == "http.upstream_error"]
    assert len(error_logs) == 1


def test_x_api_key_header_redacted_by_default() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post(
            "/embed",
            json={"texts": ["x"]},
            headers={"X-API-Key": "secret-x-api-key"},
        )
    rec = _find_error_log(logs)
    assert rec is not None
    headers = {k.lower(): v for k, v in rec["headers"].items()}
    assert headers["x-api-key"] == "***"
    assert "secret-x-api-key" not in json.dumps(rec["headers"])


def test_configured_auth_header_name_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_AUTH_HEADER_NAME", "X-Custom-Token")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post(
            "/embed",
            json={"texts": ["x"]},
            headers={"X-Custom-Token": "live-j2-token"},
        )
    rec = _find_error_log(logs)
    assert rec is not None
    headers = {k.lower(): v for k, v in rec["headers"].items()}
    assert headers["x-custom-token"] == "***"
    assert "live-j2-token" not in json.dumps(rec["headers"])


def test_streaming_request_body_unread_uses_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    def _raise(_self: httpx.Request) -> bytes:
        raise httpx.RequestNotRead()

    monkeypatch.setattr(httpx.Request, "content", property(_raise))
    client = _client(handler)
    with structlog.testing.capture_logs() as logs:
        client.post("/upload", content=iter([b"chunk-one", b"chunk-two"]))
    rec = _find_error_log(logs)
    assert rec is not None
    assert rec["http_request_payload"] == "<stream>"
    assert rec["request_truncated"] is False
