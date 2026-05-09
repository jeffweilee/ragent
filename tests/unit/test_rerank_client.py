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
        "results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
    }
    http.post.return_value = resp
    return http


def test_rerank_returns_scores():
    http = _mock_http([0.9, 0.3])
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    result = client.rerank(query="q", texts=["doc1", "doc2"], top_k=2)
    assert len(result) == 2
    assert result[0]["relevance_score"] == 0.9


def test_rerank_post_shape():
    http = _mock_http([0.5, 0.1])
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    client.rerank(query="find me", texts=["a", "b"], top_k=2)
    body = http.post.call_args[1]["json"]
    assert body["model"] == "bge-reranker-base"
    assert body["query"] == "find me"
    assert body["texts"] == ["a", "b"]
    assert body["top_k"] == 2


def test_rerank_uses_bearer_token():
    http = _mock_http([0.5])
    client = RerankClient(
        api_url="https://rerank.example.com", http=http, get_token=lambda: "secret"
    )
    client.rerank(query="q", texts=["x"], top_k=1)
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer secret"


def test_rerank_raises_upstream_service_error_on_http_error():
    http = MagicMock()
    http.post.side_effect = Exception("network error")
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.service == "rerank"
    assert exc_info.value.error_code == "RERANK_ERROR"
    assert exc_info.value.http_status == 502
    assert "network error" in str(exc_info.value)


def test_rerank_wraps_timeout_as_upstream_timeout_error():
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = RerankClient(api_url="https://rerank.example.com", http=http, get_token=lambda: "tok")
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client.rerank(query="q", texts=["x"], top_k=1)
    assert exc_info.value.error_code == "RERANK_TIMEOUT"
    assert exc_info.value.http_status == 504
