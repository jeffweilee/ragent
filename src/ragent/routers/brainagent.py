"""T-BRAIN — /brainagent/v1 router (twp-ai passthrough over the ragent-brain upstream).

Accepts a twp-ai `RunAgentInput`, forwards it verbatim to brain's `POST /run`,
and relays brain's twp-ai SSE stream back to the client. brain speaks twp-ai
natively, so this surface is a **passthrough** (option A): ragent does not inject
skills, resolve attachments, or build a `<hidden>` preamble — brain owns those.
ragent's responsibilities are strictly edge: auth, rate limit, the resumable
Redis-stream buffer (reused from `/chatagent/v3`), reconnect, and transport-error
framing (every POST failure is a `RUN_ERROR` over a 200 stream, never HTTP 4xx/5xx).

The resumable-stream plumbing (`_spawn_producer` / `_consume_stream` /
`_reconnect_stream`) is shared with — and owned by — the `/chatagent/v3` router;
this router reuses it rather than duplicating the Redis buffer logic.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from twp_ai.agent import Agent
from twp_ai.schemas import RunAgentInput

from ragent.auth.deps import get_user_id
from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.nats_publisher import NatsSessionPublisher
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import (
    _consume_stream,
    _last_user_text,
    _reconnect_stream,
    _run_error_response,
    _spawn_producer,
)
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

# user_id -> Agent. Built once in the composition root (closing over the brain
# http_client / brain_url / brain_key / timeout) and called per request, since
# BrainCaller carries the per-request X-User-Id. Simpler than v3's factory: brain
# authenticates the service by X-Brain-Key and scopes by X-User-Id, so no
# per-request JWT/token or attachment context is threaded through.
BrainAgentFactory = Callable[[str], Agent]


def create_brainagent_v1_router(
    http_client: httpx.Client,
    *,
    brain_url: str,
    brain_key: str | None = None,
    agent_factory: BrainAgentFactory,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
    timeout: float = 30.0,
    chat_stream_store: ChatStreamStore | None = None,
    nats_publisher: NatsSessionPublisher | None = None,
    stream_idle_timeout: float = 30.0,
    stream_poll_interval: float = 0.05,
    stream_producer_workers: int = 64,
) -> APIRouter:
    router = APIRouter(prefix="/brainagent/v1")

    producer_pool = (
        ThreadPoolExecutor(max_workers=stream_producer_workers, thread_name_prefix="brain-producer")
        if chat_stream_store is not None
        else None
    )

    def _rate_limited(user_id: str | None) -> bool:
        if rate_limiter is None or user_id is None:
            return False
        result = rate_limiter.check(
            f"brainagent:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        return not result.allowed

    @router.post("")
    async def brainagent_v1_post(
        body: RunAgentInput,
        request: Request,
        x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> StreamingResponse:
        user_id = x_user_id or "anonymous"

        # Model B — ragent owns the session id; mint one for a brand-new
        # conversation so brain always receives ours and echoes it in RUN_STARTED.
        if body.thread_id is None:
            body = body.model_copy(update={"thread_id": new_id()})

        if _rate_limited(x_user_id):
            logger.warning(
                "brainagent.rate_limited",
                user_id=user_id,
                error_code=HttpErrorCode.BRAINAGENT_RATE_LIMITED,
            )
            return _run_error_response(
                "Too Many Requests",
                HttpErrorCode.BRAINAGENT_RATE_LIMITED,
                body.run_id,
                body.thread_id,
            )

        agent = agent_factory(user_id)
        logger.info("brainagent.request", user_id=user_id)

        # No store wired (e.g. Redis down at boot) → legacy connection-bound
        # stream: correct, just not resumable.
        if chat_stream_store is None:
            return StreamingResponse(
                agent.run(body, body.model or ""), media_type="text/event-stream"
            )

        stream_id = new_id()
        key = chat_stream_store.key(user_id, body.thread_id or "", stream_id)
        if chat_stream_store.try_start(key) is None:
            logger.warning("brainagent.stream_store_unavailable", user_id=user_id)
            return StreamingResponse(
                agent.run(body, body.model or ""), media_type="text/event-stream"
            )
        chat_stream_store.set_current(user_id, body.thread_id or "", stream_id)
        # A HITL resume/cancel turn carries no new question, so it must not stash
        # (and replay) the previous user turn as a new one.
        if not body.resume:
            chat_stream_store.stash_user_input(key, _last_user_text(body))
        reply_expected = not (
            body.resume and all(item.status == "cancelled" for item in body.resume)
        )
        _spawn_producer(
            producer_pool,
            chat_stream_store,
            nats_publisher,
            key,
            agent,
            body,
            body.model or "",
            user_id,
            reply_expected,
        )
        return StreamingResponse(
            _consume_stream(chat_stream_store, key, "0", stream_idle_timeout, stream_poll_interval),
            media_type="text/event-stream",
        )

    @router.get("/reconnect")
    async def brainagent_v1_reconnect(
        thread_id: str,
        x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        user_id = x_user_id or "anonymous"

        def expired() -> StreamingResponse:
            return _run_error_response(
                "stream no longer resumable",
                HttpErrorCode.CHATAGENT_STREAM_EXPIRED,
                "",
                thread_id,
            )

        if chat_stream_store is None or not chat_stream_store.is_valid_cursor(last_event_id):
            return expired()
        run_id = chat_stream_store.get_current(user_id, thread_id)
        if run_id is None:
            return expired()
        key = chat_stream_store.key(user_id, thread_id, run_id)
        if not chat_stream_store.is_resumable(key):
            logger.info("brainagent.reconnect_expired", user_id=user_id, run_id=run_id)
            return expired()
        if chat_stream_store.is_done(key):
            logger.info("brainagent.reconnect_done", user_id=user_id, run_id=run_id)
            return expired()
        logger.info("brainagent.reconnect", user_id=user_id, run_id=run_id)
        user_text = (
            chat_stream_store.get_user_input(key)
            if chat_stream_store.is_from_start(last_event_id)
            else None
        )
        return StreamingResponse(
            _reconnect_stream(
                chat_stream_store,
                key,
                run_id,
                user_text,
                last_event_id or "0",
                stream_idle_timeout,
                stream_poll_interval,
            ),
            media_type="text/event-stream",
        )

    @router.post("/runs/{run_id}/cancel")
    async def brainagent_v1_cancel(
        run_id: str,
        x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> JSONResponse:
        """Cooperative cancel — owner-scoped proxy to brain's POST /runs/{id}/cancel."""
        user_id = x_user_id or "anonymous"
        headers = {"X-User-Id": user_id}
        if brain_key:
            headers["X-Brain-Key"] = brain_key
        url = f"{brain_url.rstrip('/')}/runs/{run_id}/cancel"
        try:
            resp = await run_in_threadpool(http_client.post, url, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            logger.warning("brainagent.cancel_timeout", user_id=user_id, run_id=run_id)
            return JSONResponse({"cancelled": False}, status_code=504)
        except httpx.RequestError:
            logger.warning("brainagent.cancel_error", user_id=user_id, run_id=run_id)
            return JSONResponse({"cancelled": False}, status_code=502)
        body_out = resp.json() if resp.content else {}
        return JSONResponse(body_out, status_code=resp.status_code)

    return router
