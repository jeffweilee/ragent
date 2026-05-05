"""T3.11 — POST /chat/stream: delta/done/error SSE framing (B12, S6, B6)."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.docker


def _make_app(stream_deltas=None, retrieval_docs=None, llm_error=None):
    from ragent.routers.chat import create_chat_router

    retrieval_docs = retrieval_docs or []
    stream_deltas = stream_deltas or ["Hello", " world"]

    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"source_hydrator": {"documents": retrieval_docs}}

    llm_client = MagicMock()
    if llm_error:
        llm_client.stream.side_effect = llm_error
    else:
        llm_client.stream.return_value = iter(stream_deltas)

    app = FastAPI()
    router = create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    app.include_router(router)
    return app


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.strip().splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_stream_emits_delta_then_done():
    app = _make_app(stream_deltas=["Hello", " world"])
    with TestClient(app) as client:
        resp = client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    delta_events = [e for e in events if e.get("type") == "delta"]
    done_events = [e for e in events if e.get("type") == "done"]
    assert len(delta_events) >= 1
    assert len(done_events) == 1


def test_stream_done_event_has_full_body():
    app = _make_app(stream_deltas=["Hi"])
    with TestClient(app) as client:
        resp = client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    events = _parse_sse(resp.text)
    done = next(e for e in events if e.get("type") == "done")
    assert "content" in done
    assert "model" in done
    assert "provider" in done
    assert "sources" in done


def test_stream_error_emits_error_event():
    app = _make_app(llm_error=Exception("LLM down"))
    with TestClient(app) as client:
        resp = client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    events = _parse_sse(resp.text)
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert "error_code" in error_events[0]


def test_stream_sources_null_on_empty_retrieval():
    app = _make_app(retrieval_docs=[])
    with TestClient(app) as client:
        resp = client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    events = _parse_sse(resp.text)
    done = next(e for e in events if e.get("type") == "done")
    assert done["sources"] is None


def test_chat_stream_injects_retrieved_context_into_llm_messages():
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from haystack.dataclasses import Document

    from ragent.routers.chat import create_chat_router

    doc = Document(
        content="Stream answer here",
        meta={
            "document_id": "DOC-S1",
            "source_app": "confluence",
            "source_id": "S2",
            "source_title": "Stream Wiki",
            "source_workspace": None,
        },
    )
    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"source_hydrator": {"documents": [doc]}}
    llm_client = MagicMock()
    llm_client.stream.return_value = iter(["Answer"])

    app = FastAPI()
    app.include_router(
        create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client)
    )

    with TestClient(app) as client:
        client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "stream question"}]},
            headers={"X-User-Id": "alice"},
        )

    sent_messages = llm_client.stream.call_args.kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    system_content = sent_messages[0]["content"]
    assert "QUESTION" in system_content
    assert "SUMMARY" in system_content
    assert "GENERATION" in system_content
    last_user = next(m for m in reversed(sent_messages) if m["role"] == "user")
    assert "=== CONTEXT START ===" in last_user["content"]
    assert "source_title=Stream Wiki" in last_user["content"]
    assert "Stream answer here" in last_user["content"]
    assert "stream question" in last_user["content"]
