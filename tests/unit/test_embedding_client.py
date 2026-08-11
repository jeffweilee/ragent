"""T4.3 — EmbeddingClient: POST shape, returnCode, batch interface (P-B, C8).

T-RETRY.1: there is no application-level retry. Every failure — transport,
timeout, bad `returnCode`, poison vector — surfaces on the first attempt as the
typed upstream error. Retrying a timeout re-spends a budget the call already
burned, and retrying a 4xx asks a question the upstream already answered; only
connection establishment is retried, and that lives in the shared httpx
transport (`bootstrap/composition.py`), not here.
"""

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
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        batch_size=32,
    )
    result = client.embed(["hello", "world"])
    assert result == vecs


def test_embed_post_shape():
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        batch_size=32,
    )
    client.embed(["text"])
    body = http.post.call_args[1]["json"]
    assert body["texts"] == ["text"]
    assert body["model"] == "bge-m3"
    assert body["encoding-format"] == "float"


def test_embed_post_uses_configured_model_when_provided() -> None:
    """T-EM.21: `model` constructor arg overrides the bge-m3 default so the
    same client class can serve candidate-model embed calls during B50
    migrations."""
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed-v2.example.com",
        http=http,
        get_token=lambda: "tok",
        model="bge-m3-v2",
    )
    client.embed(["text"])
    body = http.post.call_args[1]["json"]
    assert body["model"] == "bge-m3-v2"


def test_embed_post_model_defaults_to_bge_m3_when_omitted() -> None:
    """Back-compat: existing call sites that don't pass `model` keep the
    historical bge-m3 default. Pinned so a future EmbeddingClient
    refactor cannot silently switch the implicit model."""
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com", http=http, get_token=lambda: "tok"
    )
    client.embed(["text"])
    assert http.post.call_args[1]["json"]["model"] == "bge-m3"


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
    """The inner ValueError is wrapped in UpstreamServiceError per
    `00_rule.md` §API Error Honesty — on the first response, not after a retry
    round: the upstream answered, and asking again cannot change the answer."""
    http = _mock_http([[0.1]], return_code=99999)
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["text"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "returnCode" in str(exc_info.value.__cause__)
    assert http.post.call_count == 1


def test_embed_does_not_retry_a_transient_error():
    """T-RETRY.1 — a transport failure surfaces immediately. Previously this
    client burned two more attempts (plus 2 s of sleeps) before giving up, which
    tripled the load on an upstream that was already failing."""
    http = MagicMock()
    http.post.side_effect = [
        Exception("transient"),
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
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError):
        client.embed(["text"])
    assert http.post.call_count == 1


def test_embed_raises_upstream_service_error_on_first_failure():
    http = MagicMock()
    http.post.side_effect = Exception("boom")
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["text"])
    assert exc_info.value.service == "embedding"
    assert exc_info.value.error_code == "EMBEDDER_ERROR"
    assert exc_info.value.http_status == 502
    assert "boom" in str(exc_info.value)
    assert http.post.call_count == 1


def test_embed_wraps_timeout_as_upstream_timeout_error():
    """A timeout is never retried: the call already spent its whole budget, so
    a second attempt only doubles the wait the caller is holding for."""
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client.embed(["text"])
    assert exc_info.value.error_code == "EMBEDDER_TIMEOUT"
    assert exc_info.value.http_status == 504
    assert http.post.call_count == 1


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
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        batch_size=32,
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


def test_embed_honours_explicit_zero_ingest_timeout(monkeypatch):
    """T-APL.3 — explicit ingest_timeout=0 must not be swallowed by env fallback."""
    monkeypatch.setenv("EMBEDDER_INGEST_TIMEOUT_SECONDS", "30")
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        ingest_timeout=0,
        query_timeout=10,
    )
    client.embed(["text"], query=False)
    assert http.post.call_args[1]["timeout"] == 0


def test_embed_honours_explicit_zero_query_timeout(monkeypatch):
    """T-APL.3 — explicit query_timeout=0 must not be swallowed by env fallback."""
    monkeypatch.setenv("EMBEDDER_QUERY_TIMEOUT_SECONDS", "10")
    http = _mock_http([[0.1]])
    client = EmbeddingClient(
        api_url="https://embed.example.com",
        http=http,
        get_token=lambda: "tok",
        ingest_timeout=30,
        query_timeout=0,
    )
    client.embed(["text"], query=True)
    assert http.post.call_args[1]["timeout"] == 0


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
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["hello"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "zero magnitude" in str(exc_info.value.__cause__)
    # Not retried: the same input yields the same poison vector, so a second
    # call would spend another 30 s to fail identically (T-RETRY.1).
    assert http.post.call_count == 1


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
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.embed(["hello"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "non-finite" in str(exc_info.value.__cause__)
    assert http.post.call_count == 1


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
    )
    out = client.embed(["hello"])
    assert out == [[0.01, 0.02, 0.03]]
