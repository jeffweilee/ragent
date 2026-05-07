"""T4.3 — EmbeddingClient: POST shape, returnCode, retry, batch interface (P-B, C8)."""

from unittest.mock import MagicMock

import pytest

from ragent.clients.embedding import EmbeddingClient


def _mock_http(vectors: list[list[float]], return_code: int = 96200) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": return_code,
        "data": [{"embedding": v} for v in vectors],
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
    assert "texts" in body
    assert body["texts"] == ["text"]
    assert body.get("model") == "bge-m3"


def test_embed_uses_bearer_token():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "mytoken"
    )
    client.embed(["text"])
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer mytoken"


def test_embed_raises_on_bad_return_code():
    http = _mock_http([[0.1]], return_code=99999)
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "tok"
    )
    with pytest.raises(ValueError, match="returnCode"):
        client.embed(["text"])


def test_embed_retries_3_times_on_http_error():
    http = MagicMock()
    http.post.side_effect = [
        Exception("timeout"),
        Exception("timeout"),
        MagicMock(
            **{
                "raise_for_status": MagicMock(),
                "json.return_value": {"returnCode": 96200, "data": [{"embedding": [0.1]}]},
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


def test_embed_raises_after_3_failed_retries():
    http = MagicMock()
    http.post.side_effect = Exception("boom")
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(Exception, match="boom"):  # noqa: B017
        client.embed(["text"])
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
            "data": [{"embedding": [float(i + 1)]} for i in range(len(json["texts"]))],
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
        "data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}],
    }
    http.post.return_value = resp
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(ValueError, match="zero magnitude"):
        client.embed(["hello"])


def test_embed_raises_on_nan_vector() -> None:
    from unittest.mock import MagicMock

    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "data": [{"embedding": [0.1, float("nan"), 0.2]}],
    }
    http.post.return_value = resp
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(ValueError, match="non-finite"):
        client.embed(["hello"])


def test_embed_accepts_well_formed_vectors() -> None:
    from unittest.mock import MagicMock

    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "returnCode": 96200,
        "data": [{"embedding": [0.01, 0.02, 0.03]}],
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
