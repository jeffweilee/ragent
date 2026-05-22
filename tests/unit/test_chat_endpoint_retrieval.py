"""Unit tests: chat router passes top_k and min_score to run_retrieval (B-Phase)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.chat import create_chat_router


def _make_app():
    retrieval_pipeline = MagicMock()
    llm_client = MagicMock()
    llm_client.chat.return_value = {
        "content": "ok",
        "usage": {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
    }
    app = FastAPI()
    app.include_router(
        create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    )
    return app


def _capture_client(app, monkeypatch):
    calls: list = []

    def _run(*_a, **kw):
        calls.append(kw)
        return []

    monkeypatch.setattr("ragent.routers.chat.run_retrieval", _run)
    return TestClient(app), calls


def test_chat_passes_top_k_to_run_retrieval(monkeypatch):
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 7})
    assert calls, "run_retrieval was not called"
    assert calls[0].get("top_k") == 7


def test_chat_passes_min_score_to_run_retrieval(monkeypatch):
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post(
        "/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "min_score": 0.4}
    )
    assert calls, "run_retrieval was not called"
    assert calls[0].get("min_score") == pytest.approx(0.4)


def test_chat_top_k_defaults_to_DEFAULT_TOP_K_when_omitted(monkeypatch):
    from ragent.pipelines.retrieve import DEFAULT_TOP_K

    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}]})
    assert calls[0].get("top_k") == DEFAULT_TOP_K


def test_chat_min_score_defaults_to_DEFAULT_MIN_SCORE_when_omitted(monkeypatch):
    from ragent.pipelines.retrieve import DEFAULT_MIN_SCORE

    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}]})
    assert calls[0].get("min_score") == DEFAULT_MIN_SCORE


def test_chat_top_k_validation_rejects_zero(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post(
        "/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 0}
    )
    assert resp.status_code == 422


def test_chat_top_k_validation_rejects_over_200(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post(
        "/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 201}
    )
    assert resp.status_code == 422


def test_chat_min_score_validation_rejects_negative(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post(
        "/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "min_score": -0.5}
    )
    assert resp.status_code == 422


def test_stream_passes_top_k_and_min_score_to_run_retrieval(monkeypatch):
    """Streaming endpoint shares _run_retrieval — same routing applies."""
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post(
        "/chat/v1/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "top_k": 3, "min_score": 0.2},
    )
    assert calls, "run_retrieval was not called for streaming endpoint"
    assert calls[0].get("top_k") == 3
    assert calls[0].get("min_score") == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# dedupe field tests
# ---------------------------------------------------------------------------


def _make_app_with_docs(docs):
    retrieval_pipeline = MagicMock()
    llm_client = MagicMock()
    llm_client.chat.return_value = {
        "content": "ok",
        "usage": {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
    }
    app = FastAPI()
    app.include_router(
        create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    )
    return app, retrieval_pipeline


def _doc(document_id, chunk_id=None):
    from haystack.dataclasses import Document

    return Document(
        content=f"content of {document_id}",
        meta={
            "document_id": document_id,
            "chunk_id": chunk_id or document_id,
            "source_app": "app",
            "source_id": document_id,
        },
    )


def test_chat_dedupe_false_by_default(monkeypatch):
    """When dedupe is not set (default False), duplicate document_ids are NOT removed."""
    app, _ = _make_app_with_docs([])
    docs = [_doc("d1"), _doc("d1"), _doc("d2")]

    monkeypatch.setattr("ragent.routers.chat.run_retrieval", lambda *a, **kw: docs)

    client = TestClient(app)
    resp = client.post(
        "/chat/v1",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    assert len(sources) == 3  # all three chunks returned


def test_chat_dedupe_true_removes_duplicate_documents(monkeypatch):
    """When dedupe=true, only the first chunk per document_id is kept."""
    app, _ = _make_app_with_docs([])
    docs = [_doc("d1"), _doc("d1"), _doc("d2")]

    monkeypatch.setattr("ragent.routers.chat.run_retrieval", lambda *a, **kw: docs)

    client = TestClient(app)
    resp = client.post(
        "/chat/v1",
        json={"messages": [{"role": "user", "content": "hi"}], "dedupe": True},
    )
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    doc_ids = [s["source_id"] for s in sources]
    assert doc_ids.count("d1") == 1
    assert len(sources) == 2


def test_chat_dedupe_schema_field_defaults_false():
    from ragent.schemas.chat import ChatRequest

    req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    assert req.dedupe is False


def test_chat_dedupe_stream_true_removes_duplicate_documents(monkeypatch):
    """dedupe=true also applies on the streaming endpoint."""
    app, _ = _make_app_with_docs([])
    docs = [_doc("d1"), _doc("d1"), _doc("d2")]

    monkeypatch.setattr("ragent.routers.chat.run_retrieval", lambda *a, **kw: docs)

    client = TestClient(app)
    import json as _json

    lines = []
    with client.stream(
        "POST",
        "/chat/v1/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "dedupe": True},
    ) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                lines.append(_json.loads(line[6:]))

    done = next(e for e in lines if e["type"] == "done")
    assert len(done["sources"]) == 2
