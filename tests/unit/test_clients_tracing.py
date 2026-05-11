"""HTTP client OTEL spans + business logs (LLM / Embedding / Rerank)."""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from ragent.bootstrap.logging_config import configure_logging
from ragent.clients.embedding import EmbeddingClient
from ragent.clients.llm import LLMClient
from ragent.clients.rerank import RerankClient
from ragent.errors.upstream import UpstreamServiceError


@pytest.fixture()
def exporter(otel_exporter):
    return otel_exporter


@pytest.fixture(autouse=True)
def _logging_setup():
    configure_logging("ragent-test")
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


class _FakeResp:
    def __init__(self, status: int = 200, payload: Any = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.calls = 0

    def post(self, *_a, **_kw):
        self.calls += 1
        return self._resp


def _names(exporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def test_llm_chat_emits_single_span(exporter):
    payload = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }
    http = _FakeHttp(_FakeResp(200, payload))
    client = LLMClient(api_url="http://llm", http=http, get_token=lambda: "t", timeout=1.0)
    with structlog.testing.capture_logs() as logs:
        out = client.chat(messages=[{"role": "user", "content": "hi"}], model="m")
    assert out["content"] == "ok"
    assert "llm.chat" in _names(exporter)
    assert any(e.get("event") == "llm.call" for e in logs)


def test_llm_chat_records_error_on_retry_exhaustion(exporter):
    http = _FakeHttp(_FakeResp(500))
    client = LLMClient(
        api_url="http://llm", http=http, get_token=lambda: "t", timeout=1.0, sleep=lambda _s: None
    )
    with structlog.testing.capture_logs() as logs, pytest.raises(UpstreamServiceError):
        client.chat(messages=[{"role": "user", "content": "hi"}], model="m")
    assert "llm.chat" in _names(exporter)
    assert any(e.get("event") == "llm.error" for e in logs)


def test_embedding_emits_span_per_call(exporter):
    payload = {"returnCode": 96200, "returnMessage": "success", "returnData": [{"index": 0, "embedding": [0.1, 0.2]}]}
    http = _FakeHttp(_FakeResp(200, payload))
    client = EmbeddingClient(
        api_url="http://emb",
        http=http,
        get_token=lambda: "t",
        batch_size=32,
        ingest_timeout=1.0,
        query_timeout=1.0,
        sleep=lambda _s: None,
    )
    with structlog.testing.capture_logs() as logs:
        result = client.embed(["a"])
    assert result == [[0.1, 0.2]]
    assert "embedding.embed" in _names(exporter)
    assert any(e.get("event") == "embedding.call" for e in logs)


def test_rerank_emits_span(exporter):
    payload = {"returnCode": 96200, "returnMessage": "success", "returnData": [{"index": 0, "score": 0.9}]}
    http = _FakeHttp(_FakeResp(200, payload))
    client = RerankClient(api_url="http://rr", http=http, get_token=lambda: "t", timeout=1.0)
    with structlog.testing.capture_logs() as logs:
        client.rerank(query="q", texts=["a", "b"])
    assert "rerank.score" in _names(exporter)
    assert any(e.get("event") == "rerank.call" for e in logs)


def test_client_logs_do_not_contain_payload_content(exporter):
    payload = {
        "choices": [{"message": {"content": "supersecret-completion"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    http = _FakeHttp(_FakeResp(200, payload))
    client = LLMClient(api_url="http://llm", http=http, get_token=lambda: "t", timeout=1.0)
    with structlog.testing.capture_logs() as logs:
        client.chat(messages=[{"role": "user", "content": "supersecret-prompt"}], model="m")
    serialized = repr(logs)
    assert "supersecret-prompt" not in serialized
    assert "supersecret-completion" not in serialized
