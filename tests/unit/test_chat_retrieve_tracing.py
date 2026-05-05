"""OTEL span coverage for chat & retrieve routers (Haystack 2.x compatible)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ragent.bootstrap.logging_config import configure_logging
from ragent.routers.chat import create_chat_router
from ragent.routers.retrieve import create_retrieve_router


@pytest.fixture()
def exporter(otel_exporter):
    return otel_exporter


@pytest.fixture(autouse=True)
def _logging_setup():
    configure_logging("ragent-test")
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _make_doc(doc_id: str = "doc-1"):
    return SimpleNamespace(
        meta={"document_id": doc_id, "chunk_id": "c1", "title": "t"},
        content="x",
        score=0.5,
    )


class _StubPipeline:
    def __init__(self, docs):
        self._docs = docs

    def run(self, data):  # pragma: no cover - not exercised here
        return {}


class _StubLLM:
    def chat(self, **_kwargs: Any) -> dict:
        return {"content": "hello", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}


def _patch_pipelines(monkeypatch, docs):
    from ragent.pipelines import chat as pipelines_chat
    from ragent.routers import chat as chat_router_mod
    from ragent.routers import retrieve as retrieve_router_mod

    fake = lambda *_a, **_kw: list(docs)  # noqa: E731
    monkeypatch.setattr(pipelines_chat, "run_retrieval", fake, raising=True)
    monkeypatch.setattr(chat_router_mod, "run_retrieval", fake, raising=True)
    monkeypatch.setattr(retrieve_router_mod, "run_retrieval", fake, raising=True)


def _build_chat_app(monkeypatch, docs):
    _patch_pipelines(monkeypatch, docs)
    app = FastAPI()
    app.include_router(
        create_chat_router(retrieval_pipeline=_StubPipeline(docs), llm_client=_StubLLM())
    )
    return app


def _build_retrieve_app(monkeypatch, docs):
    _patch_pipelines(monkeypatch, docs)
    app = FastAPI()
    app.include_router(create_retrieve_router(retrieval_pipeline=_StubPipeline(docs)))
    return app


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def test_chat_emits_request_retrieval_llm_spans(exporter, monkeypatch):
    docs = [_make_doc()]
    app = _build_chat_app(monkeypatch, docs)
    client = TestClient(app)
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "m",
        "provider": "openai",
        "temperature": 0.0,
        "max_tokens": 16,
    }
    resp = client.post("/chat", json=body, headers={"X-User-Id": "u1"})
    assert resp.status_code == 200, resp.text
    names = _span_names(exporter)
    assert "chat.request" in names
    assert "chat.retrieval" in names
    assert "chat.llm" in names


def test_retrieve_emits_request_pipeline_spans(exporter, monkeypatch):
    docs = [_make_doc("d1"), _make_doc("d1"), _make_doc("d2")]
    app = _build_retrieve_app(monkeypatch, docs)
    client = TestClient(app)
    resp = client.post(
        "/retrieve",
        json={"query": "hello", "dedupe": True},
        headers={"X-User-Id": "u1"},
    )
    assert resp.status_code == 200, resp.text
    names = _span_names(exporter)
    assert "retrieve.request" in names
    assert "retrieve.pipeline" in names
    assert "retrieve.dedupe" in names


def test_chat_business_log_has_safe_attributes_only(exporter, monkeypatch):
    docs = [_make_doc()]
    app = _build_chat_app(monkeypatch, docs)
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/chat",
            json={
                "messages": [{"role": "user", "content": "supersecret-query"}],
                "model": "m",
                "provider": "openai",
                "temperature": 0.0,
                "max_tokens": 16,
            },
            headers={"X-User-Id": "u1"},
        )
    assert resp.status_code == 200
    biz = [e for e in logs if e.get("event") == "chat.retrieval"]
    assert biz, "expected chat.retrieval business log"
    rec = biz[0]
    # Identity + counts only — no raw content.
    assert "result_count" in rec
    assert "query_len" in rec
    serialized = repr(rec)
    assert "supersecret-query" not in serialized


def test_retrieve_dedupe_log_records_count(exporter, monkeypatch):
    docs = [_make_doc("d1"), _make_doc("d1"), _make_doc("d2")]
    app = _build_retrieve_app(monkeypatch, docs)
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        client.post("/retrieve", json={"query": "q", "dedupe": True}, headers={"X-User-Id": "u1"})
    dedupe = [e for e in logs if e.get("event") == "retrieve.dedupe"]
    assert dedupe
    rec = dedupe[0]
    assert rec.get("input_count") == 3
    assert rec.get("output_count") == 2
