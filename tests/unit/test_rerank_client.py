"""T4.7 — RerankClient: POST shape, bge-reranker-base, top_k=2 (P2 wired)."""

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


def test_rerank_raises_upstream_service_error_after_3_failed_retries():
    """Phase B — rerank retries 3× @ 2s (parity with embedding/llm) before
    wrapping the last failure in UpstreamServiceError."""
    http = MagicMock()
    http.post.side_effect = Exception("network error")
    sleep_calls: list[float] = []
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: sleep_calls.append(s),
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.service == "rerank"
    assert exc_info.value.error_code == "RERANK_ERROR"
    assert exc_info.value.http_status == 502
    assert "network error" in str(exc_info.value)
    assert http.post.call_count == 3
    assert sleep_calls == [2.0, 2.0]


def test_rerank_wraps_timeout_as_upstream_timeout_error():
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.error_code == "RERANK_TIMEOUT"
    assert exc_info.value.http_status == 504
    assert http.post.call_count == 3


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
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError):
        client.rerank(query="q", texts=["x"], top_k=1)


def test_rerank_retries_3_times_on_error():
    """Successful retry on 3rd attempt returns result without raising."""
    http = MagicMock()
    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": 0, "score": 0.9}],
    }
    http.post.side_effect = [Exception("transient"), Exception("transient"), ok_resp]
    sleep_calls: list[float] = []
    client = RerankClient(
        api_url="https://rerank.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: sleep_calls.append(s),
    )
    result = client.rerank(query="q", texts=["x"], top_k=1)
    assert result == [{"index": 0, "score": 0.9}]
    assert http.post.call_count == 3
    assert sleep_calls == [2.0, 2.0]
