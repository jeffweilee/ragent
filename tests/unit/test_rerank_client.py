"""T4.7 — RerankClient: POST shape, bge-reranker-base, top_k=2 (P2 wired).

T-RETRY.1: no application-level retry — every failure surfaces on the first
attempt. Rerank is an *optional* pipeline stage that already fails open, so
spending 94 s of retries before degrading was strictly worse than degrading now.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from ragent.clients.rerank import RerankClient
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError


def _mock_http(scores: list[float]) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": i, "score": s} for i, s in enumerate(scores)],
    }
    http.post.return_value = resp
    return http


def test_rerank_returns_scores():
    http = _mock_http([0.9, 0.3])
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    result = client.rerank(query="q", texts=["doc1", "doc2"], top_k=2)
    assert len(result) == 2
    assert result[0]["score"] == 0.9


def test_rerank_post_shape():
    http = _mock_http([0.5, 0.1])
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    client.rerank(query="find me", texts=["a", "b"], top_k=2)
    body = http.post.call_args[1]["json"]
    assert body["model"] == "bge-reranker-base"
    assert body["question"] == "find me"
    assert body["documents"] == ["a", "b"]
    assert body["top_k"] == 2


def test_rerank_uses_raw_token_no_bearer():
    http = _mock_http([0.5])
    client = RerankClient(
        api_url="https://rerank.example.com", http=http, get_token=lambda: "secret"
    )
    client.rerank(query="q", texts=["x"], top_k=1)
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "secret"


def test_rerank_honours_explicit_zero_timeout(monkeypatch):
    """T-APL.3 — explicit timeout=0 must not be swallowed by env fallback."""
    monkeypatch.setenv("RERANK_TIMEOUT_SECONDS", "30")
    http = _mock_http([0.5])
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
        timeout=0,
    )
    client.rerank(query="q", texts=["x"], top_k=1)
    assert http.post.call_args[1]["timeout"] == 0


def test_rerank_custom_auth_header_name():
    http = _mock_http([0.5])
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "secret",
        auth_header_name="X-API-Key",
    )
    client.rerank(query="q", texts=["x"], top_k=1)
    headers = http.post.call_args[1]["headers"]
    assert "Authorization" not in headers
    assert headers["X-API-Key"] == "secret"


def test_rerank_raises_upstream_service_error_on_first_failure():
    http = MagicMock()
    http.post.side_effect = Exception("network error")
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.service == "rerank"
    assert exc_info.value.error_code == "RERANK_ERROR"
    assert exc_info.value.http_status == 502
    assert "network error" in str(exc_info.value)
    assert http.post.call_count == 1


def test_rerank_wraps_timeout_as_upstream_timeout_error():
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.error_code == "RERANK_TIMEOUT"
    assert exc_info.value.http_status == 504
    assert http.post.call_count == 1


def test_rerank_unexpected_return_code_raises():
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"returnCode": 50001, "returnMessage": "model error", "returnData": []}
    http.post.return_value = resp
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError):
        client.rerank(query="q", texts=["x"], top_k=1)
    assert http.post.call_count == 1


def test_rerank_does_not_retry_a_transient_error():
    """T-RETRY.1 — a transient failure is no longer papered over by two more
    attempts; it surfaces so the caller (a fail-open pipeline stage) can degrade
    immediately instead of after 94 s."""
    http = MagicMock()
    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": 0, "score": 0.9}],
    }
    http.post.side_effect = [Exception("transient"), ok_resp]
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError):
        client.rerank(query="q", texts=["x"], top_k=1)
    assert http.post.call_count == 1
