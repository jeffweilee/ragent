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
from twp_ai.events import RunErrorEvent, UserMessageEvent, to_sse
from twp_ai.schemas import RunAgentInput

from ragent.auth.deps import get_user_id
from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.nats_publisher import NatsSessionPublisher
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
    nats_publisher: NatsSessionPublisher | None = None,
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
            # later GET /reconnect resolves the buffer via the current pointer.
            #
            # The buffer key uses a SERVER-minted stream id, never the client
            # run_id: v3 never deduplicated on run_id, so a repeated run_id must
            # still reach upstream and produce a fresh run (not silently replay the
            # previous buffer). reconnect finds the run via the current pointer, so
            # the client never needs this id.
            stream_id = new_id()
            key = chat_stream_store.key(user_id, body.thread_id or "", stream_id)
            if chat_stream_store.try_start(key) is None:
                # Stream Redis unreachable — degrade to the legacy connection-bound
                # stream so v3 chat keeps working (just not resumable this run).
                logger.warning("chatagent_v3.stream_store_unavailable", user_id=user_id)
                return StreamingResponse(
                    agent.run(body, body.model or ""), media_type="text/event-stream"
                )
            chat_stream_store.set_current(user_id, body.thread_id or "", stream_id)
            # Stash the user turn (the live stream omits it) so reconnect can
            # restore the question without relying on client storage. A HITL
            # `resume`/`cancel` turn carries no new question (upstream gets an empty
            # message), so stashing the last historical user turn would make
            # reconnect replay the previous question as a new one.
            if not body.resume:
                chat_stream_store.stash_user_input(key, _last_user_text(body))
            # A resume whose interrupts are all "cancelled" contacts no upstream and
            # yields no new reply, so it must not dot the session as unread.
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
                _consume_stream(
                    chat_stream_store,
                    key,
                    "0",
                    stream_idle_timeout,
                    stream_poll_interval,
                    user_id=user_id,
                    thread_id=body.thread_id or "",
                    nats_publisher=nats_publisher,
                ),
                media_type="text/event-stream",
            )

        @router.get("/reconnect")
        async def chatagent_v3_reconnect(
            thread_id: str,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
            last_event_id: Annotated[str | None, Header()] = None,
        ) -> StreamingResponse:
            user_id = x_user_id or "anonymous"

            def expired() -> StreamingResponse:
                return StreamingResponse(
                    _error_stream(
                        "stream no longer resumable",
                        HttpErrorCode.CHATAGENT_STREAM_EXPIRED,
                        "",
                        thread_id,
                    ),
                    media_type="text/event-stream",
                )

            # Reject a malformed Last-Event-ID up front: an arbitrary string would
            # make the XRANGE cursor raise inside the stream and 500. No store
            # wired falls through here too.
            if chat_stream_store is None or not chat_stream_store.is_valid_cursor(last_event_id):
                return expired()
            # The thread's CURRENT run is resolved server-side — a client-supplied
            # run_id could be stale (another tab/device started a newer run) and
            # resurrect an old, already-persisted turn. Owner is in the pointer key,
            # so this is also per-user scoped.
            run_id = chat_stream_store.get_current(user_id, thread_id)
            if run_id is None:
                return expired()
            key = chat_stream_store.key(user_id, thread_id, run_id)
            # is_resumable accepts a run whose producer holds the lock but hasn't
            # written its first frame yet (startup race).
            if not chat_stream_store.is_resumable(key):
                logger.info("chatagent_v3.reconnect_expired", user_id=user_id, run_id=run_id)
                return expired()
            # A FINISHED run is (within the fast upstream write) already in session,
            # so reconnect refuses it — the client loads it from GET /session, and
            # there is no buffer/session overlap to de-duplicate. Only a still-running
            # run is replayed.
            if chat_stream_store.is_done(key):
                logger.info("chatagent_v3.reconnect_done", user_id=user_id, run_id=run_id)
                return expired()
            logger.info("chatagent_v3.reconnect", user_id=user_id, run_id=run_id)
            # On a from-start replay, prepend the stashed user turn so the question
            # is restored from the server (the live stream never carried it). Use
            # is_from_start, not falsiness: "0"/"-" are truthy from-start cursors.
            # An incremental resume already has the user turn.
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
                    user_id=user_id,
                    thread_id=thread_id,
                    nats_publisher=nats_publisher,
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
            # Strip the machine-context wrapper from each session title and enrich
            # each entry with its live {running, hasNewReply} status, batched in one
            # status_many call (no store → list degrades to title-only).
            return await proxy_get(
                http_client=http_client,
                url=chatagent_sessionlist_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.sessionlist",
                transform=lambda payload: map_session_list_payload(
                    payload, _session_status_fn(chat_stream_store, user_id, payload)
                ),
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
            response = await proxy_get(
                http_client=http_client,
                url=chatagent_session_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session",
                transform=map_session_payload,
            )
            # Mark read only after a successful fetch — a 502/504 upstream failure must
            # not clear the dot for a session the user never actually saw. Publish the
            # cleared dot over NATS so the user's other tabs update without a refetch.
            if chat_stream_store is not None and response.status_code < 400:
                chat_stream_store.clear_unread(user_id, session)
                if nats_publisher is not None:
                    nats_publisher.publish(user_id, {"session": session, "hasNewReply": False})
            return response

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


def _last_user_text(body: RunAgentInput) -> str:
    for message in reversed(body.messages):
        if message.role == "user" and message.content is not None:
            return str(message.content)
    return ""


def _session_status_fn(
    store: ChatStreamStore | None, user_id: str, payload: object
) -> Callable[[str], dict[str, bool]] | None:
    """Build a per-session ``{running, hasNewReply}`` resolver for one list payload.

    Returns ``None`` when no store is wired (list degrades to title-only). Otherwise
    it collects every session id from the payload and resolves all statuses in a
    single batched ``status_many`` (2 Redis round-trips), then hands back a dict
    lookup so the mapper merges without per-entry Redis calls.
    """
    if store is None:
        return None
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    ids = (
        [
            s["session"]
            for s in sessions
            if isinstance(s, dict) and isinstance(s.get("session"), str)
        ]
        if isinstance(sessions, list)
        else []
    )
    statuses = store.status_many(user_id, ids)
    return lambda session_id: statuses.get(session_id, {})


def _reconnect_stream(
    store: ChatStreamStore,
    key: str,
    run_id: str,
    user_text: str | None,
    last_id: str,
    idle_timeout: float,
    poll_interval: float,
    *,
    user_id: str,
    thread_id: str,
    nats_publisher: NatsSessionPublisher | None,
) -> Generator[str, None, None]:
    """Replay a run for reconnect: the stashed user turn first, then the buffer."""
    if user_text:
        yield to_sse(UserMessageEvent(message_id=f"{run_id}-user", content=user_text))
    yield from _consume_stream(
        store,
        key,
        last_id,
        idle_timeout,
        poll_interval,
        user_id=user_id,
        thread_id=thread_id,
        nats_publisher=nats_publisher,
    )


def _spawn_producer(
    pool: ThreadPoolExecutor,
    store: ChatStreamStore,
    nats_publisher: NatsSessionPublisher | None,
    key: str,
    agent: Agent,
    body: RunAgentInput,
    model: str,
    user_id: str,
    reply_expected: bool,
) -> None:
    """Tee a run into the Redis Stream from a pooled background thread.

    Running off the request task (not awaited) is deliberate: it survives client
    disconnect — so the answer finishes and stays resumable within the TTL. The
    pool bounds how many can run at once. Agent.run never raises (it ends every
    run with RUN_FINISHED/RUN_ERROR), so the worst case is a finished buffer;
    mark_done always runs to close it.

    The run's start/finish also publish live session-list status (spinner on, then
    off + new-reply dot) over NATS. ``mark_done`` is the LAST step so a consumer can
    only observe the closing ``eos`` after the unread flag is already set — the list
    is never momentarily "finished but not yet unread".
    """
    pool.submit(
        _run_producer,
        store,
        nats_publisher,
        key,
        agent,
        body,
        model,
        user_id,
        body.thread_id or "",
        reply_expected,
    )


def _run_producer(
    store: ChatStreamStore,
    nats_publisher: NatsSessionPublisher | None,
    key: str,
    agent: Agent,
    body: RunAgentInput,
    model: str,
    user_id: str,
    thread_id: str,
    reply_expected: bool,
) -> None:
    """The producer body (extracted from the thread submit so it is unit-testable).

    Wrapped in a top-level guard: it runs as a fire-and-forget pool task whose Future
    is never awaited, so an escaping error (e.g. ``mark_done``'s pipeline on a Redis
    drop) would otherwise vanish with no log.
    """
    if nats_publisher is not None:
        nats_publisher.publish(user_id, {"session": thread_id, "running": True})
    try:
        try:
            for frame in agent.run(body, model):
                store.append(key, frame)
        finally:
            # A control-only resume (all interrupts cancelled) contacts no upstream and
            # produces no new reply, so it must not dot the session — only clear the
            # spinner. mark_unread is gated on an actual reply; mark_done still closes.
            if reply_expected:
                store.mark_unread(user_id, thread_id)
            if nats_publisher is not None:
                nats_publisher.publish(
                    user_id,
                    {"session": thread_id, "running": False, "hasNewReply": reply_expected},
                )
            store.mark_done(key)
    except Exception as exc:
        logger.error(
            "chatagent_v3.producer_failed",
            user_id=user_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )


def _consume_stream(
    store: ChatStreamStore,
    key: str,
    last_id: str,
    idle_timeout: float,
    poll_interval: float,
    *,
    user_id: str,
    thread_id: str,
    nats_publisher: NatsSessionPublisher | None,
) -> Generator[str, None, None]:
    """Replay buffered frames after ``last_id``, attaching each entry id as the SSE ``id:``.

    Polls with XRANGE (rather than blocking) so the same loop serves the live
    POST stream and a cross-pod reconnect. A ``None`` frame is the terminal
    sentinel; otherwise stop after ``idle_timeout`` of no progress (a producer
    that died without closing). The deadline resets on every batch, so a slow but
    live producer streams to completion.

    Draining to the ``eos`` sentinel means THIS viewer watched the reply to the end,
    so the thread is marked read (clear the dot + publish the cleared state). A client
    that disconnects mid-stream never reaches here, so a backgrounded run that finishes
    unwatched keeps its dot — exactly the active-vs-background distinction we want.
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
                store.clear_unread(user_id, thread_id)
                if nats_publisher is not None:
                    nats_publisher.publish(user_id, {"session": thread_id, "hasNewReply": False})
                return
            yield f"id: {entry_id}\n{frame}"
        deadline = time.monotonic() + idle_timeout
