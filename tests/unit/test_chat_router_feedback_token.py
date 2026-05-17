"""T-FB.10 — /chat response carries request_id + feedback_token when secret is set."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from haystack.dataclasses import Document

from ragent.routers.chat import create_chat_router
from ragent.utility.feedback_token import verify as verify_token

SECRET = "test-secret"


def _doc(document_id: str, source_id: str, source_app: str = "confluence") -> Document:
    return Document(
        content="chunk text",
        meta={
            "document_id": document_id,
            "source_id": source_id,
            "source_app": source_app,
            "source_title": "Title",
        },
        score=0.9,
    )


def _mock_retrieval_pipeline(docs: list[Document]) -> Any:
    """Stub the pipeline so run_retrieval() yields a fixed Documents list."""
    pipeline = MagicMock()
    pipeline.graph.nodes = []
    pipeline.run = MagicMock(return_value={"excerpt_truncator": {"documents": docs}})
    return pipeline


def _mock_llm(content: str = "answer") -> Any:
    llm = MagicMock()
    llm.chat = MagicMock(
        return_value={
            "content": content,
            "usage": {"promptTokens": 1, "completionTokens": 2, "totalTokens": 3},
        }
    )
    llm.stream = MagicMock(return_value=iter(["he", "llo"]))
    return llm


def _make_app(*, feedback_secret: str | None) -> tuple[TestClient, Any]:
    docs = [_doc("DOCID01", "DOC-A"), _doc("DOCID02", "DOC-B")]
    pipeline = _mock_retrieval_pipeline(docs)
    llm = _mock_llm()
    app = FastAPI()
    app.include_router(
        create_chat_router(
            retrieval_pipeline=pipeline,
            llm_client=llm,
            feedback_hmac_secret=feedback_secret,
        )
    )
    return TestClient(app), pipeline


def _chat_body() -> dict:
    return {"messages": [{"role": "user", "content": "what are the Q3 OKRs?"}]}


def test_chat_response_includes_request_id_and_feedback_token_when_secret_set():
    client, _ = _make_app(feedback_secret=SECRET)
    resp = client.post("/chat/v1", json=_chat_body(), headers={"X-User-Id": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert "request_id" in body and len(body["request_id"]) == 26
    payload = verify_token(body["feedback_token"], SECRET)
    assert payload["request_id"] == body["request_id"]
    assert payload["user_id"] == "alice"


def test_token_sources_hash_binds_actual_response_source_pairs():
    """Tampering with shown_sources at feedback time must fail HMAC.

    sources_hash is over (source_app, source_id) **pairs** so a malicious
    client cannot forge the source_app for a known source_id.
    """
    from hashlib import sha256

    client, _ = _make_app(feedback_secret=SECRET)
    resp = client.post("/chat/v1", json=_chat_body(), headers={"X-User-Id": "alice"})
    body = resp.json()
    pairs = [[s["source_app"], s["source_id"]] for s in body["sources"]]
    expected_hash = sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload = verify_token(body["feedback_token"], SECRET)
    assert payload["sources_hash"] == expected_hash


def test_no_envelope_when_feedback_disabled():
    client, _ = _make_app(feedback_secret=None)
    resp = client.post("/chat/v1", json=_chat_body(), headers={"X-User-Id": "alice"})
    body = resp.json()
    assert "request_id" not in body
    assert "feedback_token" not in body


def test_no_envelope_when_user_id_missing():
    """No X-User-Id → no token (user_id is in the HMAC payload)."""
    client, _ = _make_app(feedback_secret=SECRET)
    resp = client.post("/chat/v1", json=_chat_body())
    body = resp.json()
    assert "request_id" not in body
    assert "feedback_token" not in body


def test_two_requests_get_different_request_ids():
    client, _ = _make_app(feedback_secret=SECRET)
    headers = {"X-User-Id": "alice"}
    r1 = client.post("/chat/v1", json=_chat_body(), headers=headers).json()
    r2 = client.post("/chat/v1", json=_chat_body(), headers=headers).json()
    assert r1["request_id"] != r2["request_id"]


def test_streaming_done_event_carries_envelope():
    client, _ = _make_app(feedback_secret=SECRET)
    with client.stream(
        "POST", "/chat/v1/stream", json=_chat_body(), headers={"X-User-Id": "alice"}
    ) as resp:
        chunks = list(resp.iter_text())
    body = "".join(chunks)
    # extract the final "data: {type:done, ...}" line
    done_line = next(
        line[len("data: ") :]
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "done"' in line
    )
    done = json.loads(done_line)
    assert done["type"] == "done"
    assert "request_id" in done
    payload = verify_token(done["feedback_token"], SECRET)
    assert payload["request_id"] == done["request_id"]
