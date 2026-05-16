"""POST /feedback/v1 — closed-loop feedback ingest (T-FB.6, B51).

Verifies the HMAC-signed snapshot token from the original /chat response,
checks `source_id ∈ shown_source_ids`, re-embeds `query_text` once, then
dual-writes MariaDB `feedback` (truth) → ES `feedback_v1` (serving view).
ES failure increments the counter but the request still returns 204 —
MariaDB has the row and an offline replay job (P2) can backfill ES.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from opentelemetry import trace

from ragent.bootstrap.metrics import feedback_es_write_failed_total
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.feedback import FeedbackRequest
from ragent.utility.datetime import utcnow
from ragent.utility.feedback_token import (
    TokenExpired,
    TokenInvalid,
    TokenTampered,
    verify,
)

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _sources_hash(source_ids: list[str]) -> str:
    """Mirror /chat's sources_hash: sha256 over the ordered source_id list."""
    import json

    return sha256(json.dumps(source_ids, separators=(",", ":")).encode("utf-8")).hexdigest()


def create_feedback_router(
    *,
    feedback_repository: Any,
    embedding_client: Any,
    es_client: Any,
    hmac_secret: str,
    es_index: str = "feedback_v1",
) -> APIRouter:
    router = APIRouter(prefix="/feedback/v1")

    @router.post("", status_code=204)
    async def post_feedback(
        body: FeedbackRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> Response:
        with _tracer.start_as_current_span("feedback.request") as span:
            if x_user_id:
                span.set_attribute("user_id", x_user_id)
            span.set_attribute("vote", body.vote)

            try:
                payload = verify(body.feedback_token, hmac_secret)
            except TokenExpired:
                return problem(
                    410,
                    HttpErrorCode.FEEDBACK_TOKEN_EXPIRED,
                    "feedback token expired",
                    "Token ts is outside the 7-day window",
                )
            except (TokenTampered, TokenInvalid):
                return problem(
                    401,
                    HttpErrorCode.FEEDBACK_TOKEN_INVALID,
                    "feedback token invalid",
                    "Token failed HMAC verification or is malformed",
                )

            if payload.get("sources_hash") != _sources_hash(body.shown_source_ids):
                return problem(
                    401,
                    HttpErrorCode.FEEDBACK_TOKEN_INVALID,
                    "shown_source_ids do not match token",
                    "sources_hash mismatch — client tampered with the source list",
                )

            if body.source_id not in body.shown_source_ids:
                return problem(
                    422,
                    HttpErrorCode.FEEDBACK_SOURCE_INVALID,
                    "source_id not in shown_source_ids",
                    f"{body.source_id!r} was not among the sources shown in this request",
                )

            user_id = payload["user_id"]
            user_id_hash = sha256(user_id.encode("utf-8")).hexdigest()

            # Re-embed the query so feedback_v1 carries the same vector
            # geometry as chunks_v1 (same model + `query=True` asymmetric path).
            embeddings = await run_in_threadpool(embedding_client.embed, [body.query_text], True)
            query_embedding = embeddings[0]

            # MariaDB first — source of truth (B51).
            await feedback_repository.upsert(
                request_id=body.request_id,
                user_id=user_id,
                source_id=body.source_id,
                vote=body.vote,
                reason=body.reason.value if body.reason else None,
                position_shown=body.position_shown,
            )

            # ES second — failure is logged + counted, request still 204.
            es_id = sha256(
                f"{user_id}|{body.request_id}|{body.source_id}".encode()
            ).hexdigest()
            es_doc = {
                "request_id": body.request_id,
                "query_text": body.query_text,
                "query_embedding": query_embedding,
                "source_id": body.source_id,
                "vote": body.vote,
                "reason": body.reason.value if body.reason else None,
                "user_id_hash": user_id_hash,
                "ts": utcnow().isoformat(),
            }
            try:
                await run_in_threadpool(es_client.index, index=es_index, id=es_id, document=es_doc)
            except Exception:
                feedback_es_write_failed_total.inc()
                logger.exception(
                    "feedback.es_write_failed",
                    request_id=body.request_id,
                    source_id=body.source_id,
                )
            return Response(status_code=204)

    return router
