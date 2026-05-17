"""T-FB.6 — POST /feedback/v1 router: HMAC verify, snapshot check, dual-write.

Document identity is the ``(source_app, source_id)`` pair per B11/B35; both
go into the schema, the upsert, the ES doc, the es_id, and the HMAC
``sources_hash`` (T-FB review-fix).
"""

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
SHOWN_PAIRS: list[tuple[str, str]] = [
    ("confluence", "DOC-A"),
    ("confluence", "DOC-B"),
    ("drive", "DOC-C"),
]
SHOWN_BODY = [{"source_app": a, "source_id": s} for a, s in SHOWN_PAIRS]
SOURCES_HASH = sha256(
    json.dumps([[a, s] for a, s in SHOWN_PAIRS], separators=(",", ":")).encode("utf-8")
).hexdigest()


def _make_token(
    *,
    sources_hash: str = SOURCES_HASH,
    ts_delta: int = 0,
    user_id: str = USER_ID,
    request_id: str = REQUEST_ID,
) -> str:
    return sign(
        {
            "request_id": request_id,
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
        "shown_sources": SHOWN_BODY,
        "source_app": "confluence",
        "source_id": "DOC-A",
        "vote": 1,
        "reason": "irrelevant",
        **overrides,
    }


# --- happy path -----------------------------------------------------------


def test_happy_path_returns_204_and_writes_both_stores():
    client, repo, embed, es = _make_client()
    resp = client.post("/feedback/v1", json=_body(), headers={"X-User-Id": USER_ID})
    assert resp.status_code == 204
    repo.upsert.assert_awaited_once()
    kw = repo.upsert.await_args.kwargs
    assert kw["source_app"] == "confluence"
    assert kw["source_id"] == "DOC-A"
    embed.embed.assert_called_once_with(["what are the Q3 OKRs?"], True)
    es.index.assert_called_once()


def test_es_doc_carries_source_app_and_es_id_distinguishes_apps():
    """S42 — ES doc must include source_app (the retriever aggregates by the pair)."""
    client, _, _, es = _make_client()
    client.post("/feedback/v1", json=_body(), headers={"X-User-Id": USER_ID})
    call_a = es.index.call_args
    doc_a = call_a.kwargs["document"]
    id_a = call_a.kwargs["id"]
    assert doc_a["source_app"] == "confluence"
    assert doc_a["source_id"] == "DOC-A"

    # Same source_id under a different source_app must produce a distinct es_id
    # (mirrors MariaDB uq_user_req_app_src). Build a token whose sources_hash
    # binds the alt-app shown set so HMAC passes.
    alt_pairs = [("drive", "DOC-A"), ("confluence", "DOC-B")]
    alt_hash = sha256(
        json.dumps([[a, s] for a, s in alt_pairs], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    alt_token = _make_token(sources_hash=alt_hash)
    es.index.reset_mock()
    client.post(
        "/feedback/v1",
        json=_body(
            feedback_token=alt_token,
            shown_sources=[{"source_app": a, "source_id": s} for a, s in alt_pairs],
            source_app="drive",
            source_id="DOC-A",
        ),
        headers={"X-User-Id": USER_ID},
    )
    id_b = es.index.call_args.kwargs["id"]
    assert id_a != id_b


# --- token integrity ------------------------------------------------------


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
    """S45 — client claims different shown_sources than signed."""
    client, *_ = _make_client()
    resp = client.post(
        "/feedback/v1",
        json=_body(
            shown_sources=[
                {"source_app": "confluence", "source_id": "DOC-X"},
                {"source_app": "drive", "source_id": "DOC-Y"},
            ],
            source_app="confluence",
            source_id="DOC-X",
        ),
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"


def test_request_id_replay_rejected():
    """S50 — body request_id must equal signed request_id (codex P1)."""
    client, repo, _, es = _make_client()
    forged = _body(request_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ")  # body lies; token still valid
    resp = client.post("/feedback/v1", json=forged, headers={"X-User-Id": USER_ID})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"
    repo.upsert.assert_not_called()
    es.index.assert_not_called()


def test_cross_user_id_rejected():
    """S51 — X-User-Id must equal signed user_id (token-relay rejected)."""
    client, repo, _, es = _make_client()
    resp = client.post(
        "/feedback/v1",
        json=_body(),  # token signed for alice
        headers={"X-User-Id": "bob"},  # advisory now ENFORCED
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "FEEDBACK_TOKEN_INVALID"
    repo.upsert.assert_not_called()
    es.index.assert_not_called()


def test_missing_x_user_id_still_accepts_when_token_user_id_present():
    """Header is OPTIONAL; payload user_id is the authoritative copy."""
    client, repo, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body())  # no X-User-Id
    assert resp.status_code == 204
    assert repo.upsert.await_args.kwargs["user_id"] == USER_ID


# --- per-pair validation --------------------------------------------------


def test_voted_pair_not_in_shown_returns_422():
    """S46 — voted (source_app, source_id) pair must be in shown_sources."""
    client, *_ = _make_client()
    resp = client.post("/feedback/v1", json=_body(source_id="DOC-NOT-SHOWN"))
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "FEEDBACK_SOURCE_INVALID"


def test_voted_pair_with_wrong_source_app_rejected():
    """Right source_id, wrong source_app (only the pair is allowed)."""
    client, *_ = _make_client()
    # DOC-A is in shown under source_app=confluence; vote under source_app=drive is invalid
    resp = client.post("/feedback/v1", json=_body(source_app="drive", source_id="DOC-A"))
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "FEEDBACK_SOURCE_INVALID"


# --- schema validation ----------------------------------------------------


def test_invalid_reason_returns_422_problem_json():
    """S47 — reason outside the B52 frozen enum → FEEDBACK_VALIDATION."""
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
    client, *_ = _make_client()
    body = _body()
    del body["feedback_token"]
    resp = client.post("/feedback/v1", json=body)
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["error_code"] == "FEEDBACK_VALIDATION"


def test_missing_source_app_returns_422_problem_json():
    """Pydantic enforces top-level source_app field per spec §3.4.5."""
    client, *_ = _make_client()
    body = _body()
    del body["source_app"]
    resp = client.post("/feedback/v1", json=body)
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["error_code"] == "FEEDBACK_VALIDATION"
    assert any("source_app" in f["field"] for f in payload["errors"])


# --- idempotency + ES fail-open ------------------------------------------


def test_idempotent_vote_same_quadruple_returns_204():
    """S48 — repeat POST same (user, req, app, src) → both 204."""
    client, repo, *_ = _make_client()
    body = _body()
    r1 = client.post("/feedback/v1", json=body)
    r2 = client.post("/feedback/v1", json=body)
    assert r1.status_code == 204 and r2.status_code == 204
    assert repo.upsert.await_count == 2  # DB UNIQUE enforces idempotency, not the router


def test_es_write_failure_still_returns_204_and_increments_counter():
    """S49 — ES leg fail-open: row stays in MariaDB, counter +1, request 204."""
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
