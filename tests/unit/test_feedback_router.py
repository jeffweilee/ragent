"""T-FB.6 — POST /feedback/v1 router: HMAC verify, snapshot check, dual-write."""

from __future__ import annotations

import json
import time
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.metrics import feedback_es_write_failed_total
from ragent.routers.feedback import create_feedback_router
from ragent.utility.feedback_token import sign

SECRET = "test-feedback-hmac-secret"
USER_ID = "alice"
REQUEST_ID = "01JABCDEFGHIJKLMNOPQRSTUVW"
SHOWN = ["DOC-A", "DOC-B", "DOC-C"]
SOURCES_HASH = sha256(json.dumps(SHOWN, separators=(",", ":")).encode("utf-8")).hexdigest()


def _make_token(
    *, sources_hash: str = SOURCES_HASH, ts_delta: int = 0, user_id: str = USER_ID
) -> str:
    return sign(
        {
            "request_id": REQUEST_ID,
            "user_id": user_id,
            "sources_hash": sources_hash,
            "ts": int(time.time()) + ts_delta,
        },
        SECRET,
    )


def _make_client(es_raises: bool = False) -> tuple[TestClient, MagicMock, MagicMock, MagicMock]:
    repo = MagicMock()
    repo.upsert = AsyncMock(return_value="FEEDBACK01234567890123456789")
    embed = MagicMock()
    embed.embed = MagicMock(return_value=[[0.01] * 1024])
    es = MagicMock()
    if es_raises:
        es.index = MagicMock(side_effect=RuntimeError("ES down"))
    else:
        es.index = MagicMock(return_value={"result": "created"})
    app = FastAPI()
    app.include_router(
        create_feedback_router(
            feedback_repository=repo,
            embedding_client=embed,
            es_client=es,
            hmac_secret=SECRET,
        )
    )
    return TestClient(app), repo, embed, es


def _body(**overrides):
    return {
        "request_id": REQUEST_ID,
        "feedback_token": _make_token(),
        "query_text": "what are the Q3 OKRs?",
        "shown_source_ids": SHOWN,
        "source_id": "DOC-A",
        "vote": 1,
        "reason": "irrelevant",
        **overrides,
    }


def test_happy_path_returns_204_and_writes_both_stores():
    client, repo, embed, es = _make_client()
    resp = client.post("/feedback/v1", json=_body(), headers={"X-User-Id": USER_ID})
    assert resp.status_code == 204
    repo.upsert.assert_awaited_once()
    embed.embed.assert_called_once_with(["what are the Q3 OKRs?"], True)
    es.index.assert_called_once()


def test_tampered_token_returns_401():
    client, *_ = _make_client()
    token = _make_token()
    bad = token[:-1] + ("X" if token[-1] != "X" else "Y")
    resp = client.post("/feedback/v1", json=_body(feedback_token=bad))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"


def test_expired_token_returns_410():
    client, *_ = _make_client()
    expired = _make_token(ts_delta=-8 * 86400)
    resp = client.post("/feedback/v1", json=_body(feedback_token=expired))
    assert resp.status_code == 410
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_EXPIRED"


def test_sources_hash_mismatch_returns_401():
    client, *_ = _make_client()
    # token signed for SHOWN but request claims different shown_source_ids
    resp = client.post(
        "/feedback/v1",
        json=_body(shown_source_ids=["DOC-X", "DOC-Y"], source_id="DOC-X"),
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"


def test_source_id_not_in_shown_returns_422():
    client, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body(source_id="DOC-NOT-SHOWN"))
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "FEEDBACK_SOURCE_INVALID"


def test_invalid_reason_returns_422_problem_json():
    """S47 — reason outside the B52 frozen enum returns FEEDBACK_VALIDATION
    in RFC 9457 problem+json shape (not FastAPI's default {detail: [...]})."""
    client, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body(reason="bogus_reason"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "FEEDBACK_VALIDATION"
    assert body["status"] == 422
    assert any("reason" in f["field"] for f in body["errors"])


def test_invalid_vote_returns_422_problem_json():
    client, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body(vote=0))
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "FEEDBACK_VALIDATION"
    assert any("vote" in f["field"] for f in body["errors"])


def test_missing_required_field_returns_422_problem_json():
    """Missing top-level field also routes through FEEDBACK_VALIDATION."""
    client, *_ = _make_client()
    body = _body()
    del body["feedback_token"]
    resp = client.post("/feedback/v1", json=body)
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["error_code"] == "FEEDBACK_VALIDATION"


def test_idempotent_vote_same_triple_returns_204():
    client, repo, *_ = _make_client()
    body = _body()
    r1 = client.post("/feedback/v1", json=body)
    r2 = client.post("/feedback/v1", json=body)
    assert r1.status_code == 204 and r2.status_code == 204
    assert repo.upsert.await_count == 2  # same upsert called twice; DB enforces idempotency


def test_es_write_failure_still_returns_204_and_increments_counter():
    before = feedback_es_write_failed_total._value.get()
    client, repo, _, es = _make_client(es_raises=True)
    resp = client.post("/feedback/v1", json=_body())
    assert resp.status_code == 204
    repo.upsert.assert_awaited_once()
    es.index.assert_called_once()
    after = feedback_es_write_failed_total._value.get()
    assert after == before + 1


def test_null_reason_accepted():
    client, repo, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body(reason=None))
    assert resp.status_code == 204
    args = repo.upsert.await_args.kwargs
    assert args["reason"] is None
