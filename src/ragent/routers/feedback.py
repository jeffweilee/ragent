"""POST /feedback/v1 — closed-loop feedback ingest (T-FB.6, B55).

Verifies the HMAC-signed snapshot token from the original /chat response,
checks that the request quadruple ``(user_id, request_id, (source_app, source_id))``
is **fully bound** to the token (so a single token cannot be replayed under a
different user_id or different request_id to flood the table), then
dual-writes MariaDB `feedback` (truth) → ES `feedback_v1` (serving view).
ES failure increments the counter but the request still returns 204 —
MariaDB has the row and an offline replay job (P2) can backfill ES.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
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
    compute_sources_hash,
    verify,
)

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _validation_problem(errors: list[dict]) -> Response:
    """Convert Pydantic validation errors into RFC 9457 problem+json with
    `error_code=FEEDBACK_VALIDATION` per spec §3.4.5 (T-FB.6, mirror of
    `routers/ingest._IngestRoute`)."""
    fields = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in errors]
    return problem(
        422,
        HttpErrorCode.FEEDBACK_VALIDATION,
        "feedback request validation failed",
        "; ".join(f"{f['field']}: {f['message']}" for f in fields),
        errors=fields,
    )


class _FeedbackRoute(APIRoute):
    """Wrap the route handler so FastAPI's RequestValidationError becomes
    problem+json with the spec-declared `FEEDBACK_VALIDATION` error_code,
    rather than the default `{"detail":[...]}` shape (T-FB.6).
    """

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Any) -> Any:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return _validation_problem(exc.errors())

        return handler


def create_feedback_router(
    *,
    feedback_repository: Any,
    embedding_client: Any,
    es_client: Any,
    hmac_secret: str,
    es_index: str = "feedback_v1",
) -> APIRouter:
    router = APIRouter(prefix="/feedback/v1", route_class=_FeedbackRoute)

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

            # request_id and user_id are signed; the wire copy in the body /
            # header must match (PR #80 review, codex P1).  Without these
            # checks a stolen / replayed token could be reused with arbitrary
            # request_id values to spam the (user, request, app, source)
            # idempotency key, overweighting a source.
            if body.request_id != payload.get("request_id"):
                return problem(
                    401,
                    HttpErrorCode.FEEDBACK_TOKEN_INVALID,
                    "request_id does not match token",
                    "request_id in body must equal the request_id signed at /chat time",
                )
            token_user_id = payload["user_id"]
            if x_user_id is not None and x_user_id != token_user_id:
                return problem(
                    401,
                    HttpErrorCode.FEEDBACK_TOKEN_INVALID,
                    "X-User-Id does not match token",
                    "Token was minted for a different user — cross-user reuse rejected",
                )

            shown_pairs = [(s.source_app, s.source_id) for s in body.shown_sources]
            if payload.get("sources_hash") != compute_sources_hash(shown_pairs):
                return problem(
                    401,
                    HttpErrorCode.FEEDBACK_TOKEN_INVALID,
                    "shown_sources do not match token",
                    "sources_hash mismatch — client tampered with the source list",
                )

            voted_pair = (body.source_app, body.source_id)
            if voted_pair not in set(shown_pairs):
                return problem(
                    422,
                    HttpErrorCode.FEEDBACK_SOURCE_INVALID,
                    "source not in shown_sources",
                    f"(source_app={body.source_app!r}, source_id={body.source_id!r})"
                    " was not among the sources shown in this request",
                )

            user_id = token_user_id
            user_id_hash = sha256(user_id.encode("utf-8")).hexdigest()

            # Re-embed the query so feedback_v1 carries the same vector
            # geometry as chunks_v1 (same model + `query=True` asymmetric path).
            embeddings = await run_in_threadpool(embedding_client.embed, [body.query_text], True)
            query_embedding = embeddings[0]

            # MariaDB first — source of truth (B55).
            await feedback_repository.upsert(
                request_id=body.request_id,
                user_id=user_id,
                source_app=body.source_app,
                source_id=body.source_id,
                vote=body.vote,
                reason=body.reason.value if body.reason else None,
                position_shown=body.position_shown,
            )

            # ES second — failure is logged + counted, request still 204. The
            # ES _id key includes source_app so the per-revision idempotency
            # mirrors the MariaDB unique key.
            es_id = sha256(
                f"{user_id}|{body.request_id}|{body.source_app}|{body.source_id}".encode()
            ).hexdigest()
            es_doc = {
                "request_id": body.request_id,
                "query_text": body.query_text,
                "query_embedding": query_embedding,
                "source_app": body.source_app,
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
                    source_app=body.source_app,
                    source_id=body.source_id,
                )
            return Response(status_code=204)

    return router
