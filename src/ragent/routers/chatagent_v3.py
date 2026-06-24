"""T-CAv3 — /chatagent/v3 router (twp-ai protocol proxy over the v2 upstream).

Accepts a twp-ai `RunAgentInput`, proxies to `CHATAGENT_API_URL` (shared with
v2), and streams the upstream response back as a twp-ai SSE event stream. All
failures — rate-limit, upstream error, timeout — surface as a `RUN_ERROR` event
over a 200 stream, never as an HTTP 4xx/5xx code.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from twp_ai.agent import Agent
from twp_ai.events import RunErrorEvent, to_sse
from twp_ai.schemas import RunAgentInput

from ragent.auth.deps import get_user_id
from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.routers._chatagent_proxy import proxy_get, proxy_write
from ragent.schemas.chatagent import SessionDeleteRequest, SessionRenameRequest
from ragent.services.chatagent_session import map_session_list_payload, map_session_payload
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

# (user_id, user_token) -> Agent. Built once in the composition root (closing
# over the upstream http_client/api_url/ap_name/auth/timeout) and called per
# request, since the underlying caller carries per-request user/token state
# and so cannot be injected as a singleton Agent instance.
AgentFactory = Callable[[str, str], Agent]


def create_chatagent_v3_router(
    http_client: httpx.Client,
    chatagent_ap_name: str,
    chatagent_auth: str | None = None,
    chatagent_api_url: str | None = None,
    chatagent_sessionlist_api_url: str | None = None,
    chatagent_session_api_url: str | None = None,
    *,
    agent_factory: AgentFactory | None = None,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
    jwt_header: str = "X-Auth-Token",
    timeout: float = 30.0,
    chat_stream_store: ChatStreamStore | None = None,
    stream_idle_timeout: float = 30.0,
    stream_poll_interval: float = 0.05,
    stream_producer_workers: int = 64,
) -> APIRouter:
    router = APIRouter(prefix="/chatagent/v3")

    # Bounded pool for the decoupled producers: caps concurrent generation
    # threads so a burst of POSTs cannot spawn threads without limit. Threads are
    # created lazily on submit, so an idle app holds none. Only built when the
    # store is wired (otherwise the legacy connection-bound path is used).
    producer_pool = (
        ThreadPoolExecutor(max_workers=stream_producer_workers, thread_name_prefix="v3-producer")
        if chat_stream_store is not None
        else None
    )

    _headers: dict[str, str] = {"Authorization": chatagent_auth} if chatagent_auth else {}

    def _rate_limited(user_id: str | None) -> bool:
        if rate_limiter is None or user_id is None:
            return False
        result = rate_limiter.check(
            f"chatagent:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        return not result.allowed

    if chatagent_api_url is not None:

        @router.post("")
        async def chatagent_v3_post(
            body: RunAgentInput,
            request: Request,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> StreamingResponse:
            user_id = x_user_id or "anonymous"

            # Model B — ragent owns the session id. When the client omits it (a
            # brand-new conversation), mint one here so the upstream always
            # receives OUR session (it never mints its own) and the assigned id is
            # echoed back in RUN_STARTED for the client to reuse. Resolve before
            # any streaming path so RUN_STARTED / RUN_ERROR never carry a null id.
            if body.thread_id is None:
                body = body.model_copy(update={"thread_id": new_id()})

            if _rate_limited(x_user_id):
                logger.warning(
                    "chatagent_v3.rate_limited",
                    user_id=user_id,
                    error_code=HttpErrorCode.CHATAGENT_RATE_LIMITED,
                )
                return StreamingResponse(
                    _error_stream(
                        "Too Many Requests",
                        HttpErrorCode.CHATAGENT_RATE_LIMITED,
                        body.run_id,
                        body.thread_id,
                    ),
                    media_type="text/event-stream",
                )

            raw_token = request.headers.get(jwt_header.lower()) or ""
            assert agent_factory is not None  # this route only registers when it is
            agent = agent_factory(user_id, raw_token)
            logger.info("chatagent_v3.request", user_id=user_id)

            # No store wired (e.g. Redis down at boot): fall back to the legacy
            # connection-bound stream — correct, just not resumable.
            if chat_stream_store is None:
                return StreamingResponse(
                    agent.run(body, body.model or ""), media_type="text/event-stream"
                )

            # Resumable path: a background producer tees the run into a Redis
            # Stream independent of this connection (so generation completes even
            # if the client refreshes); the response consumes that buffer. A
            # later GET /reconnect consumes the same buffer.
            key = chat_stream_store.key(user_id, body.thread_id or "", body.run_id)
            started = chat_stream_store.try_start(key)
            if started is None:
                # Stream Redis unreachable — degrade to the legacy connection-bound
                # stream so v3 chat keeps working (just not resumable this run).
                logger.warning("chatagent_v3.stream_store_unavailable", user_id=user_id)
                return StreamingResponse(
                    agent.run(body, body.model or ""), media_type="text/event-stream"
                )
            if started:
                _spawn_producer(
                    producer_pool, chat_stream_store, key, agent, body, body.model or ""
                )
            return StreamingResponse(
                _consume_stream(
                    chat_stream_store, key, "0", stream_idle_timeout, stream_poll_interval
                ),
                media_type="text/event-stream",
            )

        @router.get("/reconnect")
        async def chatagent_v3_reconnect(
            thread_id: str,
            run_id: str,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
            last_event_id: Annotated[str | None, Header()] = None,
        ) -> StreamingResponse:
            user_id = x_user_id or "anonymous"
            # Reject a malformed Last-Event-ID up front: an arbitrary string would
            # make the XRANGE cursor raise inside the stream and 500. No store
            # wired falls through here too.
            if chat_stream_store is None or not chat_stream_store.is_valid_cursor(last_event_id):
                return StreamingResponse(
                    _error_stream(
                        "stream no longer resumable",
                        HttpErrorCode.CHATAGENT_STREAM_EXPIRED,
                        run_id,
                        thread_id,
                    ),
                    media_type="text/event-stream",
                )
            # Owner is baked into the key, so a missing key also covers another
            # user reconnecting to a run that is not theirs. is_resumable also
            # accepts a run whose producer holds the lock but hasn't written yet.
            key = chat_stream_store.key(user_id, thread_id, run_id)
            if not chat_stream_store.is_resumable(key):
                logger.info("chatagent_v3.reconnect_expired", user_id=user_id, run_id=run_id)
                return StreamingResponse(
                    _error_stream(
                        "stream no longer resumable",
                        HttpErrorCode.CHATAGENT_STREAM_EXPIRED,
                        run_id,
                        thread_id,
                    ),
                    media_type="text/event-stream",
                )
            logger.info("chatagent_v3.reconnect", user_id=user_id, run_id=run_id)
            return StreamingResponse(
                _consume_stream(
                    chat_stream_store,
                    key,
                    last_event_id or "0",
                    stream_idle_timeout,
                    stream_poll_interval,
                ),
                media_type="text/event-stream",
            )

    if chatagent_sessionlist_api_url is not None:

        @router.get("/sessionList")
        async def chatagent_v3_session_list(
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
            startTime: str | None = None,
            endTime: str | None = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            params: dict[str, str] = {"user": user_id, "apName": chatagent_ap_name}
            if startTime:
                params["startTime"] = startTime
            if endTime:
                params["endTime"] = endTime
            # strip the machine-context wrapper from each session title.
            return await proxy_get(
                http_client=http_client,
                url=chatagent_sessionlist_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.sessionlist",
                transform=map_session_list_payload,
            )

    if chatagent_session_api_url is not None:

        @router.get("/session")
        async def chatagent_v3_session(
            session: str,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            params = {"user": user_id, "apName": chatagent_ap_name, "session": session}
            # v3 reshapes the persisted history: twp-ai roles + <hidden> stripped.
            return await proxy_get(
                http_client=http_client,
                url=chatagent_session_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session",
                transform=map_session_payload,
            )

        @router.put("/session")
        async def chatagent_v3_session_rename(
            body: SessionRenameRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await proxy_write(
                http_client=http_client,
                method="PUT",
                url=chatagent_session_api_url,
                payload={
                    "session": body.session,
                    "sessionName": body.sessionName,
                    "apName": chatagent_ap_name,
                    "user": user_id,
                },
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session.rename",
            )

        @router.delete("/session")
        async def chatagent_v3_session_delete(
            body: SessionDeleteRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await proxy_write(
                http_client=http_client,
                method="DELETE",
                url=chatagent_session_api_url,
                payload={"session": body.session, "apName": chatagent_ap_name, "user": user_id},
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session.delete",
            )

    return router


def _error_stream(
    message: str, code: HttpErrorCode, run_id: str, thread_id: str | None
) -> Generator[str, None, None]:
    yield to_sse(RunErrorEvent(message=message, code=code, run_id=run_id, thread_id=thread_id))


def _spawn_producer(
    pool: ThreadPoolExecutor,
    store: ChatStreamStore,
    key: str,
    agent: Agent,
    body: RunAgentInput,
    model: str,
) -> None:
    """Tee a run into the Redis Stream from a pooled background thread.

    Running off the request task (not awaited) is deliberate: it survives client
    disconnect — so the answer finishes and stays resumable within the TTL. The
    pool bounds how many can run at once. Agent.run never raises (it ends every
    run with RUN_FINISHED/RUN_ERROR), so the worst case is a finished buffer;
    mark_done always runs to close it.
    """

    def _produce() -> None:
        try:
            for frame in agent.run(body, model):
                store.append(key, frame)
        finally:
            store.mark_done(key)

    pool.submit(_produce)


def _consume_stream(
    store: ChatStreamStore,
    key: str,
    last_id: str,
    idle_timeout: float,
    poll_interval: float,
) -> Generator[str, None, None]:
    """Replay buffered frames after ``last_id``, attaching each entry id as the SSE ``id:``.

    Polls with XRANGE (rather than blocking) so the same loop serves the live
    POST stream and a cross-pod reconnect. A ``None`` frame is the terminal
    sentinel; otherwise stop after ``idle_timeout`` of no progress (a producer
    that died without closing). The deadline resets on every batch, so a slow but
    live producer streams to completion.
    """
    cursor = last_id
    deadline = time.monotonic() + idle_timeout
    while time.monotonic() < deadline:
        entries = store.read_after(key, cursor)
        if not entries:
            time.sleep(poll_interval)
            continue
        for entry_id, frame in entries:
            cursor = entry_id
            if frame is None:
                return
            yield f"id: {entry_id}\n{frame}"
        deadline = time.monotonic() + idle_timeout
