"""T4.3 — EmbeddingClient: POST shape, returnCode, retry, batch interface (P-B, C8)."""

from unittest.mock import MagicMock

import httpx
import pytest

from ragent.clients.embedding import EmbeddingClient
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError


def _mock_http(vectors: list[list[float]], return_code: int = 96200) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": return_code,
        "returnMessage": "success",
        "returnData": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
    }
    http.post.return_value = resp
    return http


def test_embed_single_batch_returns_vectors():
    vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    http = _mock_http(vecs)
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "tok"
    )
    result = client.embed(["hello", "world"])
    assert result == vecs


def test_embed_post_shape():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "tok"
    )
    client.embed(["text"])
    body = http.post.call_args[1]["json"]
    assert body["texts"] == ["text"]
    assert body["model"] == "bge-m3"
    assert body["encoding-format"] == "float"


def test_embed_uses_raw_token_no_bearer():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "mytoken"
    )
    client.embed(["text"])
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "mytoken"


def test_embed_custom_auth_header_name():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "mytoken",
        auth_header_name="X-API-Key",
    )
    client.embed(["text"])
    headers = http.post.call_args[1]["headers"]
    assert "Authorization" not in headers
    assert headers["X-API-Key"] == "mytoken"


def test_embed_raises_on_bad_return_code():
    """After 3 retries the inner ValueError is wrapped in UpstreamServiceError
    per `00_rule.md` §API Error Honesty (retry-exhausted upstream failure)."""
    http = _mock_http([[0.1]], return_code=99999)
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["text"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "returnCode" in str(exc_info.value.__cause__)


def test_embed_retries_3_times_on_http_error():
    http = MagicMock()
    http.post.side_effect = [
        Exception("timeout"),
        Exception("timeout"),
        MagicMock(
            **{
                "raise_for_status": MagicMock(),
                "json.return_value": {
                    "returnCode": 96200,
                    "returnMessage": "success",
                    "returnData": [{"index": 0, "embedding": [0.1]}],
                },
            }
        ),
    ]
    sleep_calls: list[float] = []
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: sleep_calls.append(s),
    )
    result = client.embed(["text"])
    assert result == [[0.1]]
    assert http.post.call_count == 3
    assert len(sleep_calls) == 2
    assert all(s == 1.0 for s in sleep_calls)


def test_embed_raises_upstream_service_error_after_3_failed_retries():
    http = MagicMock()
    http.post.side_effect = Exception("boom")
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["text"])
    assert exc_info.value.service == "embedding"
    assert exc_info.value.error_code == "EMBEDDER_ERROR"
    assert exc_info.value.http_status == 502
    assert "boom" in str(exc_info.value)
    assert http.post.call_count == 3


def test_embed_wraps_timeout_as_upstream_timeout_error():
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client.embed(["text"])
    assert exc_info.value.error_code == "EMBEDDER_TIMEOUT"
    assert exc_info.value.http_status == 504
    assert http.post.call_count == 3


def test_embed_batches_by_batch_size(monkeypatch):
    """32 texts → 1 batch; 33 texts → 2 batches (batch_size=32 default)."""
    calls: list[list[str]] = []

    def fake_post(url, json, headers, timeout):
        calls.append(json["texts"])
        mock = MagicMock()
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {
            "returnCode": 96200,
            "returnMessage": "success",
            "returnData": [
                {"index": i, "embedding": [float(i + 1)]} for i in range(len(json["texts"]))
            ],
        }
        return mock

    http = MagicMock()
    http.post.side_effect = fake_post
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "tok"
    )

    texts_32 = [f"t{i}" for i in range(32)]
    result = client.embed(texts_32)
    assert len(result) == 32
    assert len(calls) == 1

    calls.clear()
    texts_33 = [f"t{i}" for i in range(33)]
    result2 = client.embed(texts_33)
    assert len(result2) == 33
    assert len(calls) == 2


def test_embed_ingest_uses_ingest_timeout():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        ingest_timeout=30,
        query_timeout=10,
    )
    client.embed(["text"], query=False)
    timeout = http.post.call_args[1]["timeout"]
    assert timeout == 30


def test_embed_query_uses_query_timeout():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        ingest_timeout=30,
        query_timeout=10,
    )
    client.embed(["text"], query=True)
    timeout = http.post.call_args[1]["timeout"]
    assert timeout == 10


def test_embed_raises_on_zero_magnitude_vector() -> None:
    """ES dense_vector cosine rejects zero-magnitude — refuse before write."""
    from unittest.mock import MagicMock

    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": 0, "embedding": [0.0, 0.0, 0.0, 0.0]}],
    }
    http.post.return_value = resp
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["hello"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "zero magnitude" in str(exc_info.value.__cause__)


def test_embed_raises_on_nan_vector() -> None:
    from unittest.mock import MagicMock

    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": 0, "embedding": [0.1, float("nan"), 0.2]}],
    }
    http.post.return_value = resp
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["hello"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "non-finite" in str(exc_info.value.__cause__)


def test_embed_accepts_well_formed_vectors() -> None:
    from unittest.mock import MagicMock

    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "returnMessage": "success",
        "returnData": [{"index": 0, "embedding": [0.01, 0.02, 0.03]}],
    }
    http.post.return_value = resp
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    out = client.embed(["hello"])
    assert out == [[0.01, 0.02, 0.03]]
