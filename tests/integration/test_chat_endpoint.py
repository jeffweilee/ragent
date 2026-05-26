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
            "/chat/v1",
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
            "/chat/v1",
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
            "/chat/v1",
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
        resp = client.post("/chat/v1", json={}, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422


def test_chat_invalid_provider_returns_422():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
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
            "/chat/v1",
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
    assert "<context>" in last_user["content"]
    assert "</context>" in last_user["content"]
    assert "The answer is 42" in last_user["content"]
    assert "What is the answer?" in last_user["content"]
    # Raw metadata is hidden from the model — only the index label is emitted
    assert "source_title=Deep Thought" not in last_user["content"]


def test_chat_no_docs_uses_rag_system_prompt_and_injects_empty_context():
    """Even when retrieval returns no docs, the RAG system prompt is used and an empty-context
    sentinel is injected — the boundary is never silently removed."""
    app, llm_client = _make_app(retrieval_docs=[], llm_content="I don't know")

    with TestClient(app) as client:
        client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": "mystery question"}]},
            headers={"X-User-Id": "alice"},
        )

    sent_messages = llm_client.chat.call_args.kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    # RAG system prompt (not the generic default) is used even with empty retrieval
    assert "QUESTION" in sent_messages[0]["content"]
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert "(The context is empty.)" in last_user["content"]


# ---------------------------------------------------------------------------
# T-CH.I1 — POST /chat/v1 {retrieve:false}: intent+pipeline skipped, sources=[]
# ---------------------------------------------------------------------------


def test_retrieve_false_skips_pipeline():
    """When retrieve=false the intent-detection LLM call and retrieval pipeline are both
    skipped; the response sources field is an empty list (not null)."""
    retrieval_docs = []
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": retrieval_docs}}

    llm_client = MagicMock()
    llm_client.chat.return_value = {"content": "Sure, here you go!", "usage": llm_usage}

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    user_msg = "<context>\n[資料來源 #1]\nsome context\n---\n</context>\n\nWhat does this say?"
    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": user_msg}], "retrieve": False},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # sources must be an empty list, not null
    assert body["sources"] == []
    # retrieval pipeline must not have been called
    retrieval_pipeline.run.assert_not_called()
    # only ONE llm call: the main chat (not intent detection)
    assert llm_client.chat.call_count == 1
    # user message is NOT double-wrapped with a second <context> block
    sent_messages = llm_client.chat.call_args.kwargs["messages"]
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert last_user["content"].count("<context>") == 1


# ---------------------------------------------------------------------------
# T-CH.I2 — POST /chat/v1/stream {retrieve:false}: done frame sources=[]
# ---------------------------------------------------------------------------


def test_stream_retrieve_false_sources_empty():
    """Streaming: retrieve=false skips pipeline; done frame has sources=[]."""
    retrieval_pipeline = MagicMock()

    llm_client = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}
    llm_client.stream.return_value = iter(["Hello", " world"])

    # stream call pulls usage from usage_out list; set it via side_effect on stream
    def _stream_side_effect(messages, model, temperature, max_tokens, usage_out):
        usage_out.append(llm_usage)
        return iter(["Hello", " world"])

    llm_client.stream.side_effect = _stream_side_effect

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1/stream",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "retrieve": False,
            },
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    # parse SSE frames
    import json as _json

    frames = [
        _json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    done_frame = next(f for f in frames if f.get("type") == "done")
    assert done_frame["sources"] == []
    retrieval_pipeline.run.assert_not_called()


# ---------------------------------------------------------------------------
# T-CH.I3 — POST /chat/v1 intent=GREETING: pipeline skipped, sources=[]
# ---------------------------------------------------------------------------


def test_greeting_intent_skips_retrieval():
    """When intent detection returns GREETING, retrieval is skipped and sources=[]."""
    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": []}}

    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _llm_side_effect(**kwargs):
        if kwargs.get("max_tokens") == 10:  # intent detection call
            return {"content": "GREETING"}
        return {"content": "Hi there!", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _llm_side_effect

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": "你好！"}]},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == []
    retrieval_pipeline.run.assert_not_called()


# ---------------------------------------------------------------------------
# T-CH.I4 — POST /chat/v1 intent=QUESTION: retrieval runs
# ---------------------------------------------------------------------------


def test_question_intent_runs_retrieval():
    """When intent detection returns QUESTION, retrieval pipeline is executed."""
    from haystack.dataclasses import Document

    doc = Document(
        content="The answer is 42",
        meta={
            "document_id": "DOC1",
            "source_app": "confluence",
            "source_id": "S1",
            "source_title": "Deep Thought",
            "source_meta": None,
        },
    )
    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": [doc]}}

    llm_usage = {"promptTokens": 20, "completionTokens": 10, "totalTokens": 30}

    def _llm_side_effect(**kwargs):
        if kwargs.get("max_tokens") == 10:  # intent detection call
            return {"content": "QUESTION"}
        return {"content": "The answer is 42 [1]", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _llm_side_effect

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": "what is the answer?"}]},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # retrieval ran and populated sources
    assert body["sources"] is not None
    assert len(body["sources"]) == 1
    retrieval_pipeline.run.assert_called_once()
