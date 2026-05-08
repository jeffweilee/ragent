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
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": retrieval_docs}}

    llm_client = MagicMock()
    llm_client.chat.return_value = {"content": llm_content, "usage": llm_usage}

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)
    return app, llm_client


def test_chat_returns_200_with_correct_shape():
    app, _ = _make_app()
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
    app, _ = _make_app(retrieval_docs=[])
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
            "source_meta": None,
        },
    )
    app, _ = _make_app(retrieval_docs=[doc])
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
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post("/chat", json={}, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422


def test_chat_invalid_provider_returns_422():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}], "provider": "anthropic"},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 422


def test_chat_injects_retrieved_context_into_llm_messages():
    from haystack.dataclasses import Document

    doc = Document(
        content="The answer is 42",
        meta={
            "document_id": "DOC42",
            "source_app": "confluence",
            "source_id": "S1",
            "source_title": "Deep Thought",
            "source_meta": None,
        },
    )
    app, llm_client = _make_app(retrieval_docs=[doc], llm_content="42 [Deep Thought]")

    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "What is the answer?"}]},
            headers={"X-User-Id": "alice"},
        )

    sent_messages = llm_client.chat.call_args.kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    system_content = sent_messages[0]["content"]
    assert "QUESTION" in system_content
    assert "SUMMARY" in system_content
    assert "GENERATION" in system_content
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert "=== CONTEXT START ===" in last_user["content"]
    assert "source_title=Deep Thought" in last_user["content"]
    assert "The answer is 42" in last_user["content"]
    assert "What is the answer?" in last_user["content"]


def test_chat_no_docs_uses_default_system_prompt_unchanged_and_does_not_wrap_user_message():
    app, llm_client = _make_app(retrieval_docs=[], llm_content="I don't know")

    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "mystery question"}]},
            headers={"X-User-Id": "alice"},
        )

    sent_messages = llm_client.chat.call_args.kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    assert sent_messages[0]["content"].startswith("You are a helpful assistant")
    assert "markdown" in sent_messages[0]["content"].lower()
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert "=== CONTEXT START ===" not in last_user["content"]
