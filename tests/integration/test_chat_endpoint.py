"""T3.9 — POST /chat: 200 JSON with content/usage/model/provider/sources (B12, S6a-S6e)."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.chat import create_chat_router

pytestmark = pytest.mark.docker


def _make_app(retrieval_docs=None, llm_content="Hello!", llm_usage=None):
    retrieval_docs = retrieval_docs or []
    llm_usage = llm_usage or {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15}

    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"source_hydrator": {"documents": retrieval_docs}}

    llm_client = MagicMock()
    llm_client.chat.return_value = {"content": llm_content, "usage": llm_usage}

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)
    return app


def test_chat_returns_200_with_correct_shape():
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "content" in body
    assert "usage" in body
    assert "model" in body
    assert "provider" in body
    assert "sources" in body


def test_chat_sources_null_when_retrieval_empty():
    app = _make_app(retrieval_docs=[])
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    assert resp.json()["sources"] is None


def test_chat_sources_populated_with_doc_metadata():
    from haystack.dataclasses import Document

    doc = Document(
        content="some text",
        meta={
            "document_id": "DOC001",
            "source_app": "confluence",
            "source_id": "S1",
            "source_title": "Title",
            "source_workspace": None,
        },
    )
    app = _make_app(retrieval_docs=[doc])
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] is not None
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["type"] == "knowledge"
    assert source["source_app"] == "confluence"


def test_chat_missing_messages_returns_422():
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/chat", json={}, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422


def test_chat_invalid_provider_returns_422():
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}], "provider": "anthropic"},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 422
