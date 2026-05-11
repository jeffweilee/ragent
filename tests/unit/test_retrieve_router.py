"""Retrieve router: source_meta in response, top_k/min_score params, response schema."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from haystack.dataclasses import Document

from ragent.pipelines.chat import run_retrieval
from ragent.routers.retrieve import create_retrieve_router


def _make_doc(
    doc_id: str = "doc-1",
    source_meta: str | None = None,
    source_app: str = "confluence",
    source_id: str = "SRC-1",
):
    return SimpleNamespace(
        meta={
            "document_id": doc_id,
            "source_app": source_app,
            "source_id": source_id,
            "source_meta": source_meta,
            "source_title": "My Title",
            "source_url": "https://example.com",
            "mime_type": "text/plain",
            "raw_content": "some excerpt text",
        },
        content="some excerpt text",
        score=0.9,
    )


@pytest.fixture()
def app():
    pipeline = MagicMock()
    _app = FastAPI()
    _app.include_router(create_retrieve_router(retrieval_pipeline=pipeline))
    return _app


def _client(app, monkeypatch, docs):
    monkeypatch.setattr("ragent.routers.retrieve.run_retrieval", lambda *_a, **_kw: list(docs))
    return TestClient(app)


def _client_capture(app, monkeypatch, calls):
    def _run(*_a, **kw):
        calls.append(kw)
        return []

    monkeypatch.setattr("ragent.routers.retrieve.run_retrieval", _run)
    return TestClient(app)


# ---------------------------------------------------------------------------
# source_meta in response
# ---------------------------------------------------------------------------


def test_chunk_response_includes_source_meta(app, monkeypatch):
    doc = _make_doc(source_meta="engineering")
    client = _client(app, monkeypatch, [doc])
    resp = client.post("/retrieve", json={"query": "test"})
    assert resp.status_code == 200
    chunk = resp.json()["chunks"][0]
    assert chunk["source_meta"] == "engineering"


def test_chunk_response_source_meta_none_when_not_set(app, monkeypatch):
    doc = _make_doc(source_meta=None)
    client = _client(app, monkeypatch, [doc])
    resp = client.post("/retrieve", json={"query": "test"})
    assert resp.status_code == 200
    chunk = resp.json()["chunks"][0]
    assert chunk["source_meta"] is None


# ---------------------------------------------------------------------------
# Response schema shape
# ---------------------------------------------------------------------------


def test_response_has_all_chunk_fields(app, monkeypatch):
    client = _client(app, monkeypatch, [_make_doc()])
    resp = client.post("/retrieve", json={"query": "q"})
    assert resp.status_code == 200
    chunk = resp.json()["chunks"][0]
    for field in (
        "document_id",
        "source_app",
        "source_id",
        "source_meta",
        "type",
        "source_title",
        "source_url",
        "mime_type",
        "excerpt",
    ):
        assert field in chunk, f"missing field: {field}"


def test_empty_result_returns_empty_chunks_list(app, monkeypatch):
    client = _client(app, monkeypatch, [])
    resp = client.post("/retrieve", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json() == {"chunks": []}


# ---------------------------------------------------------------------------
# top_k param
# ---------------------------------------------------------------------------


def test_top_k_passed_to_run_retrieval(app, monkeypatch):
    calls: list = []
    client = _client_capture(app, monkeypatch, calls)
    client.post("/retrieve", json={"query": "q", "top_k": 5})
    assert calls, "run_retrieval was not called"
    assert calls[0].get("top_k") == 5


def test_top_k_defaults_to_configured_value(app, monkeypatch):
    from ragent.pipelines.chat import DEFAULT_TOP_K

    calls: list = []
    client = _client_capture(app, monkeypatch, calls)
    client.post("/retrieve", json={"query": "q"})
    assert calls[0].get("top_k") == DEFAULT_TOP_K


def test_top_k_must_be_at_least_one(app, monkeypatch):
    client = _client(app, monkeypatch, [])
    resp = client.post("/retrieve", json={"query": "q", "top_k": 0})
    assert resp.status_code == 422


def test_top_k_capped_at_200(app, monkeypatch):
    client = _client(app, monkeypatch, [])
    resp = client.post("/retrieve", json={"query": "q", "top_k": 201})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# min_score param
# ---------------------------------------------------------------------------


def test_min_score_passed_to_run_retrieval(app, monkeypatch):
    calls: list = []
    client = _client_capture(app, monkeypatch, calls)
    client.post("/retrieve", json={"query": "q", "min_score": 0.5})
    assert calls[0].get("min_score") == pytest.approx(0.5)


def test_min_score_defaults_to_none(app, monkeypatch):
    calls: list = []
    client = _client_capture(app, monkeypatch, calls)
    client.post("/retrieve", json={"query": "q"})
    assert calls[0].get("min_score") is None


def test_min_score_must_be_non_negative(app, monkeypatch):
    client = _client(app, monkeypatch, [])
    resp = client.post("/retrieve", json={"query": "q", "min_score": -0.1})
    assert resp.status_code == 422


def test_min_score_zero_accepted(app, monkeypatch):
    calls: list = []
    client = _client_capture(app, monkeypatch, calls)
    resp = client.post("/retrieve", json={"query": "q", "min_score": 0.0})
    assert resp.status_code == 200
    assert calls[0].get("min_score") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# run_retrieval post-retrieval score filtering
# (score_threshold is not a valid ES retriever run() param — filtering is
# applied after pipeline.run() on the returned documents)
# ---------------------------------------------------------------------------


def _fake_pipeline(docs: list[Document]):
    """Minimal pipeline stub whose run() returns docs via excerpt_truncator."""
    pipeline = MagicMock()
    pipeline.graph.nodes = []
    pipeline.run.return_value = {"excerpt_truncator": {"documents": docs}}
    return pipeline


def _doc_with_score(score: float | None) -> Document:
    return dataclasses.replace(Document(content="x"), score=score)


def test_run_retrieval_min_score_filters_below_threshold():
    docs = [_doc_with_score(0.8), _doc_with_score(0.3), _doc_with_score(0.5)]
    result = run_retrieval(_fake_pipeline(docs), query="q", min_score=0.5)
    scores = [d.score for d in result]
    assert scores == [0.8, 0.5]


def test_run_retrieval_min_score_none_returns_all():
    docs = [_doc_with_score(0.1), _doc_with_score(0.9)]
    result = run_retrieval(_fake_pipeline(docs), query="q", min_score=None)
    assert len(result) == 2


def test_run_retrieval_min_score_drops_none_score_docs():
    docs = [_doc_with_score(None), _doc_with_score(0.7)]
    result = run_retrieval(_fake_pipeline(docs), query="q", min_score=0.5)
    assert len(result) == 1
    assert result[0].score == pytest.approx(0.7)


def test_run_retrieval_score_threshold_not_passed_to_pipeline():
    """score_threshold must NOT appear in inputs — ES retrievers don't accept it."""
    pipeline = _fake_pipeline([])
    run_retrieval(pipeline, query="q", min_score=0.5)
    call_inputs = pipeline.run.call_args[0][0]
    for component_inputs in call_inputs.values():
        assert "score_threshold" not in component_inputs


def test_run_retrieval_top_k_caps_output_regardless_of_pipeline():
    """Hard cap enforced post-pipeline — joiner may return more than top_k."""
    docs = [_doc_with_score(float(i)) for i in range(13)]  # pipeline returns 13
    result = run_retrieval(_fake_pipeline(docs), query="q", top_k=10)
    assert len(result) == 10


def test_run_retrieval_top_k_none_returns_all():
    docs = [_doc_with_score(float(i)) for i in range(13)]
    result = run_retrieval(_fake_pipeline(docs), query="q", top_k=None)
    assert len(result) == 13


def test_run_retrieval_top_k_applied_after_min_score():
    """min_score filters first, then top_k caps — user gets top K above threshold."""
    docs = [_doc_with_score(0.9), _doc_with_score(0.8), _doc_with_score(0.1), _doc_with_score(0.7)]
    result = run_retrieval(_fake_pipeline(docs), query="q", top_k=2, min_score=0.5)
    assert len(result) == 2
    assert all(d.score >= 0.5 for d in result)
