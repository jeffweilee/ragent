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


def test_chat_sources_empty_list_when_retrieval_ran_but_no_hits():
    """sources=[] when retrieval ran but returned no documents (not null — null means skipped)."""
    app, _ = _make_app(retrieval_docs=[])
    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


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
# T-CH.I1 / T-CH2 — POST /chat/v1 {context_mode:"caller"}: pipeline skipped, sources=null
# ---------------------------------------------------------------------------


def test_caller_mode_skips_pipeline():
    """context_mode='caller' skips retrieval pipeline; sources=null (retrieval never ran)."""
    retrieval_docs = []
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": retrieval_docs}}

    llm_client = MagicMock()

    # intent detection call + main chat call
    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "QUESTION"}
        return {"content": "Sure, here you go!", "usage": llm_usage}

    llm_client.chat.side_effect = _side

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    user_msg = "<context>\n[資料來源 #1]\nsome context\n---\n</context>\n\nWhat does this say?"
    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={
                "messages": [{"role": "user", "content": user_msg}],
                "context_mode": "caller",
            },
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # sources=null: retrieval was skipped (not ran-but-empty)
    assert body["sources"] is None
    # retrieval pipeline must not have been called
    retrieval_pipeline.run.assert_not_called()
    # user message is NOT double-wrapped with a second <context> block
    sent_messages = llm_client.chat.call_args.kwargs["messages"]
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert last_user["content"].count("<context>") == 1


# ---------------------------------------------------------------------------
# T-CH.I2 / T-CH2 — POST /chat/v1/stream {context_mode:"caller"}: done frame sources=null
# ---------------------------------------------------------------------------


def test_stream_caller_mode_sources_null():
    """Streaming: context_mode='caller' skips pipeline; done frame sources=null."""
    retrieval_pipeline = MagicMock()

    llm_client = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}
    # intent detection returns QUESTION; stream returns content
    llm_client.chat.return_value = {"content": "QUESTION"}
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
                "context_mode": "caller",
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
    # sources=null: retrieval was skipped
    assert done_frame["sources"] is None
    retrieval_pipeline.run.assert_not_called()


# ---------------------------------------------------------------------------
# T-CH.I3 — POST /chat/v1 intent=GREETING: pipeline skipped, sources=null
# ---------------------------------------------------------------------------


def test_greeting_intent_skips_retrieval():
    """When intent detection returns GREETING, retrieval is skipped and sources=null."""
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
    # sources=null: retrieval was skipped (GREETING intent)
    assert body["sources"] is None
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


# ---------------------------------------------------------------------------
# T-CH2.R4 — intent detection always runs regardless of context_mode
# ---------------------------------------------------------------------------


def test_intent_detection_runs_for_caller_mode():
    """context_mode='caller' still runs intent detection (to select prompt + temperature).
    The intent LLM call (max_tokens=10) must always be made."""
    retrieval_pipeline = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    call_args_list = []

    def _side(*, messages, model, temperature, max_tokens):
        call_args_list.append(max_tokens)
        if max_tokens == 10:
            return {"content": "QUESTION"}
        return {"content": "answer", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={"messages": [{"role": "user", "content": "hi"}], "context_mode": "caller"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    # Two LLM calls: intent detection (max_tokens=10) + main chat
    assert 10 in call_args_list, "Intent detection call (max_tokens=10) must always run"
    assert llm_client.chat.call_count == 2


# ---------------------------------------------------------------------------
# T-CH2.I1 — context_mode='caller' + QUESTION: no [N] citation in system prompt
# ---------------------------------------------------------------------------


def test_caller_mode_no_citation_in_system_prompt():
    """context_mode='caller' + QUESTION intent: system prompt must NOT contain [N] citation
    rules — caller manages their own context; sources=null prevents broken citation UX."""
    retrieval_pipeline = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "QUESTION"}
        return {"content": "Based on the context provided…", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "<context>\n[資料來源 #1]\nsome doc\n---\n</context>\n\nWhat is this?"
                        ),
                    }
                ],
                "context_mode": "caller",
            },
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] is None

    # The system prompt sent to the main LLM must NOT contain citation rules
    main_chat_call = next(
        c for c in llm_client.chat.call_args_list if c.kwargs.get("max_tokens") != 10
    )
    sys_msg = next(m for m in main_chat_call.kwargs["messages"] if m["role"] == "system")
    # No [N] citation rule text (CITATION RULE or similar) in the no-citation prompt
    assert "CITATION RULE" not in sys_msg["content"]


# ---------------------------------------------------------------------------
# T-CH2.I2 — temperature=None + intent → auto temperature
# ---------------------------------------------------------------------------


def test_auto_temperature_used_for_greeting():
    """When temperature=None, the GREETING intent's auto temperature (0.8) is used."""
    retrieval_pipeline = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "GREETING"}
        return {"content": "Hi there!", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

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
    # The main chat call must have used temperature=0.8 (GREETING auto)
    main_call = next(c for c in llm_client.chat.call_args_list if c.kwargs.get("max_tokens") != 10)
    assert main_call.kwargs["temperature"] == pytest.approx(0.8)


def test_explicit_temperature_overrides_intent():
    """When caller provides explicit temperature, it overrides the intent-based default."""
    retrieval_pipeline = MagicMock()
    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "GREETING"}
        return {"content": "Hi!", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={
                "messages": [{"role": "user", "content": "你好！"}],
                "temperature": 0.3,
            },
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    main_call = next(c for c in llm_client.chat.call_args_list if c.kwargs.get("max_tokens") != 10)
    assert main_call.kwargs["temperature"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# T-CH2.I3 — context_mode='force' + GREETING: retrieval runs
# ---------------------------------------------------------------------------


def test_force_mode_runs_retrieval_for_greeting():
    """context_mode='force' runs retrieval even when intent=GREETING."""
    from haystack.dataclasses import Document

    doc = Document(
        content="welcome info",
        meta={
            "document_id": "DOC1",
            "source_app": "app",
            "source_id": "S1",
            "source_title": "T",
            "source_meta": None,
        },
    )
    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": [doc]}}

    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "GREETING"}
        return {"content": "Hi!", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post(
            "/chat/v1",
            json={
                "messages": [{"role": "user", "content": "你好！"}],
                "context_mode": "force",
            },
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    body = resp.json()
    retrieval_pipeline.run.assert_called_once()
    assert body["sources"] is not None
    assert len(body["sources"]) == 1


# ---------------------------------------------------------------------------
# T-CH2 — citation normalization: 【N】→[N] in response content
# ---------------------------------------------------------------------------


def test_fullwidth_citation_normalized_to_ascii():
    """LLM output containing 【N】 brackets must be normalized to [N] before returning."""
    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": []}}

    llm_usage = {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}

    def _side(*, messages, model, temperature, max_tokens):
        if max_tokens == 10:
            return {"content": "QUESTION"}
        # LLM drifted to full-width brackets
        return {"content": "根據資料【1】，答案是 42。", "usage": llm_usage}

    llm_client = MagicMock()
    llm_client.chat.side_effect = _side

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
    assert "【" not in body["content"]
    assert "[1]" in body["content"]
