"""T-FB.11 — end-to-end feedback loop: chat → feedback → next chat sees boost.

Wires the real chat + feedback routers in-process with mocked clients so
the HMAC token round-trip, source_id binding, and dual-write contracts
all run on production code paths. ES + MariaDB are stubbed to capture
calls; the embedder is deterministic so kNN-replay assertions are stable.

Updated for the T-FB review-fix: document identity is the
``(source_app, source_id)`` pair; request_id + user_id are bound by the
token and enforced at /feedback time.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from haystack.dataclasses import Document

from ragent.routers.chat import create_chat_router
from ragent.routers.feedback import create_feedback_router

SECRET = "loop-integration-secret"

DOC_A = Document(
    content="OKR planning chunk",
    meta={
        "document_id": "DOCID01ABCD",
        "source_id": "DOC-A",
        "source_app": "confluence",
        "source_title": "OKR Planning",
    },
    score=0.9,
)
DOC_B = Document(
    content="roadmap chunk",
    meta={
        "document_id": "DOCID02ABCD",
        "source_id": "DOC-B",
        "source_app": "confluence",
        "source_title": "Roadmap",
    },
    score=0.8,
)


def _retrieval_pipeline(docs: list[Document]) -> Any:
    pipeline = MagicMock()
    pipeline.graph.nodes = []
    pipeline.run = MagicMock(return_value={"excerpt_truncator": {"documents": docs}})
    return pipeline


def _llm() -> Any:
    llm = MagicMock()
    llm.chat = MagicMock(
        return_value={
            "content": "answer",
            "usage": {"promptTokens": 1, "completionTokens": 2, "totalTokens": 3},
        }
    )
    return llm


@pytest.fixture
def loop():
    """A tiny app wiring real chat + feedback routers + a shared in-memory ES stub."""

    docs_initial = [DOC_A, DOC_B]
    pipeline = _retrieval_pipeline(docs_initial)

    repo = MagicMock()
    repo.upsert = AsyncMock(return_value="FEEDBACKID01ABCDEFGH123456")

    embed = MagicMock()
    embed.embed = MagicMock(return_value=[[0.5] * 1024])

    es_writes: list[dict] = []
    es = MagicMock()

    def _index(**kwargs):
        es_writes.append(kwargs)
        return {"result": "created"}

    es.index = MagicMock(side_effect=_index)

    app = FastAPI()
    app.include_router(
        create_chat_router(
            retrieval_pipeline=pipeline,
            llm_client=_llm(),
            feedback_hmac_secret=SECRET,
        )
    )
    app.include_router(
        create_feedback_router(
            feedback_repository=repo,
            embedding_client=embed,
            es_client=es,
            hmac_secret=SECRET,
        )
    )
    return {
        "client": TestClient(app),
        "pipeline": pipeline,
        "repo": repo,
        "embed": embed,
        "es": es,
        "es_writes": es_writes,
    }


def _post_chat(client, user_id: str = "alice") -> dict:
    return client.post(
        "/chat/v1",
        json={"messages": [{"role": "user", "content": "what are our Q3 OKRs?"}]},
        headers={"X-User-Id": user_id},
    ).json()


def _shown_from(chat_body: dict) -> list[dict]:
    return [
        {"source_app": s["source_app"], "source_id": s["source_id"]} for s in chat_body["sources"]
    ]


def test_chat_emits_token_then_feedback_round_trip_writes_both_stores(loop):
    chat_body = _post_chat(loop["client"])
    assert "request_id" in chat_body and "feedback_token" in chat_body
    voted = chat_body["sources"][0]
    shown = _shown_from(chat_body)

    fb_resp = loop["client"].post(
        "/feedback/v1",
        json={
            "request_id": chat_body["request_id"],
            "feedback_token": chat_body["feedback_token"],
            "query_text": "what are our Q3 OKRs?",
            "shown_sources": shown,
            "source_app": voted["source_app"],
            "source_id": voted["source_id"],
            "vote": 1,
            "reason": "irrelevant",
        },
        headers={"X-User-Id": "alice"},
    )
    assert fb_resp.status_code == 204

    # MariaDB upsert called with correct keys including source_app.
    loop["repo"].upsert.assert_awaited_once()
    kw = loop["repo"].upsert.await_args.kwargs
    assert kw["request_id"] == chat_body["request_id"]
    assert kw["user_id"] == "alice"
    assert kw["source_app"] == voted["source_app"]
    assert kw["source_id"] == voted["source_id"]
    assert kw["vote"] == 1
    assert kw["reason"] == "irrelevant"

    # ES feedback_v1 doc carries source_app + user_id_hash; user_id never leaks plaintext.
    assert len(loop["es_writes"]) == 1
    es_doc = loop["es_writes"][0]["document"]
    assert es_doc["source_app"] == voted["source_app"]
    assert es_doc["source_id"] == voted["source_id"]
    assert es_doc["vote"] == 1
    assert len(es_doc["query_embedding"]) == 1024
    assert "user_id" not in es_doc
    assert "user_id_hash" in es_doc and len(es_doc["user_id_hash"]) == 64


def test_three_distinct_users_each_get_a_distinct_es_id(loop):
    """Closes the loop conceptually: 3 different users each cast +1 on the same source."""
    chat_body = _post_chat(loop["client"])
    voted = chat_body["sources"][0]

    for u in ("alice", "bob", "carol"):
        cb = _post_chat(loop["client"], user_id=u)
        loop["client"].post(
            "/feedback/v1",
            json={
                "request_id": cb["request_id"],
                "feedback_token": cb["feedback_token"],
                "query_text": "what are our Q3 OKRs?",
                "shown_sources": _shown_from(cb),
                "source_app": cb["sources"][0]["source_app"],
                "source_id": cb["sources"][0]["source_id"],
                "vote": 1,
            },
            headers={"X-User-Id": u},
        )

    assert loop["repo"].upsert.await_count == 3
    assert len(loop["es_writes"]) == 3
    # All three ES writes target the same (source_app, source_id) pair.
    assert {
        (w["document"]["source_app"], w["document"]["source_id"]) for w in loop["es_writes"]
    } == {(voted["source_app"], voted["source_id"])}
    # Each write got a distinct ES _id (sha256 includes user|request|app|source).
    assert len({w["id"] for w in loop["es_writes"]}) == 3


def test_cross_user_token_relay_is_now_rejected(loop):
    """Codex P1: token-relay (Alice's token used by Bob) must 401, not silently write under Alice.

    The previous "advisory X-User-Id" behaviour was a bug — a stolen / leaked token
    could be reused by any caller. Now X-User-Id MUST equal the signed user_id.
    """
    chat_body = _post_chat(loop["client"], user_id="alice")
    resp = loop["client"].post(
        "/feedback/v1",
        json={
            "request_id": chat_body["request_id"],
            "feedback_token": chat_body["feedback_token"],
            "query_text": "what are our Q3 OKRs?",
            "shown_sources": _shown_from(chat_body),
            "source_app": chat_body["sources"][0]["source_app"],
            "source_id": chat_body["sources"][0]["source_id"],
            "vote": 1,
        },
        headers={"X-User-Id": "bob"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"
    loop["repo"].upsert.assert_not_called()


def test_request_id_replay_rejected(loop):
    """Codex P1: body request_id must equal signed payload request_id."""
    chat_body = _post_chat(loop["client"])
    resp = loop["client"].post(
        "/feedback/v1",
        json={
            "request_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",  # forged
            "feedback_token": chat_body["feedback_token"],
            "query_text": "what are our Q3 OKRs?",
            "shown_sources": _shown_from(chat_body),
            "source_app": chat_body["sources"][0]["source_app"],
            "source_id": chat_body["sources"][0]["source_id"],
            "vote": 1,
        },
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"
    loop["repo"].upsert.assert_not_called()


def test_shown_sources_tamper_rejected(loop):
    chat_body = _post_chat(loop["client"])
    resp = loop["client"].post(
        "/feedback/v1",
        json={
            "request_id": chat_body["request_id"],
            "feedback_token": chat_body["feedback_token"],
            "query_text": "what are our Q3 OKRs?",
            "shown_sources": [
                {"source_app": "fake", "source_id": "DOC-FAKE-A"},
                {"source_app": "fake", "source_id": "DOC-FAKE-B"},
            ],
            "source_app": "fake",
            "source_id": "DOC-FAKE-A",
            "vote": 1,
        },
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"
    loop["repo"].upsert.assert_not_called()
    assert loop["es_writes"] == []
