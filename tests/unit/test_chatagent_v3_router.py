"""T-CAv3.4 — chatagent v3 router (twp-ai protocol proxy) unit tests."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twp_ai.events import (
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
    to_sse,
)

from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.nats_publisher import NatsSessionPublisher
from ragent.clients.rate_limiter import RateLimiter, RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router
from ragent.services.document_artifact_resolver import DocumentArtifactResolver
from tests.helpers import done_line as _done_line
from tests.helpers import msg_line as _msg_line
from tests.helpers import parse_sse_events as _events
from tests.helpers import parse_sse_ids as _ids
from tests.helpers import real_agent_factory as _real_agent_factory
from tests.helpers import resp_mock as _resp_mock


def _make_app(
    *,
    rate_limiter: RateLimiter | None = None,
    chat_stream_store: ChatStreamStore | None = None,
    nats_publisher: NatsSessionPublisher | None = None,
    stream_idle_timeout: float = 3.0,
    sessionlist_url: str | None = None,
    session_url: str | None = None,
    document_artifact_resolver=None,
    attachment_max_files: int | None = None,
):
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    router = create_chatagent_v3_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_auth="Bearer up",
        chatagent_api_url="http://upstream",
        chatagent_sessionlist_api_url=sessionlist_url,
        chatagent_session_api_url=session_url,
        agent_factory=_real_agent_factory(
            http_mock, api_url="http://upstream", ap_name="TestAP", auth="Bearer up"
        ),
        rate_limiter=rate_limiter,
        chat_stream_store=chat_stream_store,
        nats_publisher=nats_publisher,
        stream_idle_timeout=stream_idle_timeout,
        document_artifact_resolver=document_artifact_resolver,
        attachment_max_files=attachment_max_files,
    )
    app.include_router(router)
    return app, http_mock


def _json_resp(payload: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _store() -> ChatStreamStore:
    return ChatStreamStore(fakeredis.FakeStrictRedis(decode_responses=True))


def _seed_running_run(
    store: ChatStreamStore,
    *,
    user: str = "alice",
    thread: str = "thread_1",
    run: str = "run_1",
    user_text: str | None = "What are the features?",
    deltas: tuple[str, ...] = ("hi",),
    message_id: str = "m1",
) -> tuple[str, list[str]]:
    """Seed an in-flight (no eos) buffered run directly; returns (key, frame_ids).

    reconnect serves only still-running runs (a finished run → expired), so tests
    that exercise the streaming path build a buffer with no terminal sentinel.
    """
    key = ChatStreamStore.key(user, thread, run)
    store.set_current(user, thread, run)
    store.try_start(key)
    if user_text is not None:
        store.stash_user_input(key, user_text)
    ids = [store.append(key, to_sse(RunStartedEvent(run_id=run, thread_id=thread)))]
    ids.append(store.append(key, to_sse(TextMessageStartEvent(message_id=message_id))))
    for d in deltas:
        ids.append(
            store.append(key, to_sse(TextMessageContentEvent(message_id=message_id, delta=d)))
        )
    return key, ids


def _run_input() -> dict:
    return {
        "threadId": "thread_1",
        "runId": "run_1",
        "messages": [{"id": "m1", "role": "user", "content": "What are the features?"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": None,
    }


def test_v3_mints_thread_id_when_omitted() -> None:
    # Model B: client omits threadId on a new conversation; ragent mints it,
    # echoes it in RUN_STARTED, and sends it as the upstream session.
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])
    body = _run_input()
    del body["threadId"]

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    events = _events(r.text)
    minted = events[0]["threadId"]
    assert minted  # non-empty minted id surfaced to the client
    sent = http_mock.build_request.call_args.kwargs["json"]
    assert sent["metadata"]["session"] == minted  # upstream gets ours, not its own


def test_v3_uses_supplied_thread_id() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    events = _events(r.text)
    assert events[0]["threadId"] == "thread_1"
    sent = http_mock.build_request.call_args.kwargs["json"]
    assert sent["metadata"]["session"] == "thread_1"


def test_v3_streams_twp_ai_event_lifecycle() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("The ", message_id="msg-1"),
            _msg_line("features", message_id="msg-1"),
            _done_line(),
        ]
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = _events(r.text)
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"] == [
        "The ",
        "features",
    ]
    assert events[0]["runId"] == "run_1"
    assert events[0]["threadId"] == "thread_1"


def test_v3_planner_message_streams_reasoning_events() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("Planning ", message_id="plan-1", agent_type="planner"),
            _msg_line("steps", message_id="plan-1", agent_type="planner"),
            _msg_line("The answer.", message_id="sum-1", agent_type="summarizer"),
            _done_line(),
        ]
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert [e["delta"] for e in events if e["type"] == "REASONING_MESSAGE_CONTENT"] == [
        "Planning ",
        "steps",
    ]


def test_v3_injects_server_metadata() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock([_done_line()])

    with TestClient(app) as client:
        client.post(
            "/chatagent/v3",
            json=_run_input(),
            headers={"X-User-Id": "bob", "X-Auth-Token": "tok-bob"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok-bob"
    assert payload["metadata"]["session"] == "thread_1"
    assert payload["inputData"]["message"] == "What are the features?"
    assert payload["stream"] is True


def test_v3_resolves_attachment_ids_into_hidden_block() -> None:
    """T-CAT.W1 — attachment_ids are resolved (async) before the agent runs."""
    resolver = AsyncMock(spec=DocumentArtifactResolver)
    resolver.resolve.return_value = '[{"attachmentId": "att-1"}]'
    app, http_mock = _make_app(document_artifact_resolver=resolver)
    http_mock.send.return_value = _resp_mock([_done_line()])
    body = _run_input()
    body["attachmentIds"] = ["att-1"]

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    resolver.resolve.assert_called_once_with(["att-1"])
    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    assert '<attachments>[{"attachmentId": "att-1"}]</attachments>' in message


def test_v3_skips_attachment_resolution_when_resolver_unwired() -> None:
    """No DocumentArtifactResolver wired (attachment subsystem disabled) — no crash."""
    app, http_mock = _make_app(document_artifact_resolver=None)
    http_mock.send.return_value = _resp_mock([_done_line()])
    body = _run_input()
    body["attachmentIds"] = ["att-1"]

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    assert "<attachments>" not in message


def test_v3_skips_attachment_resolution_when_no_ids_present() -> None:
    """attachment_ids omitted — resolver must not be called even if wired."""
    resolver = AsyncMock(spec=DocumentArtifactResolver)
    app, http_mock = _make_app(document_artifact_resolver=resolver)
    http_mock.send.return_value = _resp_mock([_done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    resolver.resolve.assert_not_called()


def test_v3_too_many_attachments_emits_run_error_not_http_413() -> None:
    """T-CAT.W16 — attachment_ids over ATTACHMENT_MAX_FILES is a RUN_ERROR, not HTTP 413."""
    resolver = AsyncMock(spec=DocumentArtifactResolver)
    app, http_mock = _make_app(document_artifact_resolver=resolver, attachment_max_files=2)
    body = _run_input()
    body["attachmentIds"] = ["att-1", "att-2", "att-3"]

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.ATTACHMENT_TOO_MANY_FILES
    assert events[-1]["runId"] == "run_1"
    resolver.resolve.assert_not_called()
    http_mock.send.assert_not_called()


def test_v3_attachment_count_at_limit_is_allowed() -> None:
    """Exactly ATTACHMENT_MAX_FILES ids is allowed (boundary, not over)."""
    resolver = AsyncMock(spec=DocumentArtifactResolver)
    resolver.resolve.return_value = None
    app, http_mock = _make_app(document_artifact_resolver=resolver, attachment_max_files=2)
    http_mock.send.return_value = _resp_mock([_done_line()])
    body = _run_input()
    body["attachmentIds"] = ["att-1", "att-2"]

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    resolver.resolve.assert_called_once_with(["att-1", "att-2"])


def test_v3_rate_limited_emits_run_error_not_http_429() -> None:
    rl_mock = MagicMock(spec=RateLimiter)
    result = MagicMock(spec=RateLimitResult)
    result.allowed = False
    result.reset_at = 9999999999.0
    rl_mock.check.return_value = result

    app, http_mock = _make_app(rate_limiter=rl_mock)
    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "dave"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
    assert events[-1]["runId"] == "run_1"
    http_mock.send.assert_not_called()


def test_v3_upstream_error_emits_run_error() -> None:
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.RequestError("conn refused")

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_v3_resumable_post_streams_same_lifecycle_with_event_ids() -> None:
    # With a store wired the POST streams through the Redis buffer: the twp-ai
    # event sequence is unchanged, but every frame now carries an SSE `id:` so
    # the client can resume from it.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("The ", message_id="msg-1"),
            _msg_line("answer", message_id="msg-1"),
            _done_line(),
        ]
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    events = _events(r.text)
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    # One id per emitted frame, all distinct — the resume cursor.
    ids = _ids(r.text)
    assert len(ids) == len(events)
    assert len(set(ids)) == len(ids)


def test_v3_reconnect_on_finished_run_is_expired() -> None:
    # Once the run finishes (eos written), reconnect returns expired — the client
    # loads the completed turn from GET /session instead, so there is no
    # buffer/session overlap to de-duplicate.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})  # completes
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert _events(r.text)[-1]["code"] == HttpErrorCode.CHATAGENT_STREAM_EXPIRED


def test_v3_reconnect_resumes_after_last_event_id() -> None:
    # Drop after some frames of a STILL-RUNNING run, then reconnect with the last
    # id: only strictly-later frames replay; an incremental resume omits the user turn.
    store = _store()
    app, _ = _make_app(chat_stream_store=store, stream_idle_timeout=0.2)
    _key, ids = _seed_running_run(store, deltas=("The ", "answer"))

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice", "Last-Event-ID": ids[1]},
        )

    replayed = _events(r.text)
    assert "RUN_STARTED" not in [e["type"] for e in replayed]
    assert "USER_MESSAGE" not in [e["type"] for e in replayed]
    assert all(i > ids[1] for i in _ids(r.text))


def test_v3_reconnect_resolves_current_run_and_replays_user_turn() -> None:
    # From-start reconnect needs only thread_id; the server resolves the current
    # run and prepends the stashed user turn (the live stream never carried it).
    store = _store()
    app, _ = _make_app(chat_stream_store=store, stream_idle_timeout=0.2)
    _seed_running_run(store, user_text="What are the features?")

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    events = _events(r.text)
    assert events[0]["type"] == "USER_MESSAGE"
    assert events[0]["content"] == "What are the features?"
    assert events[0]["role"] == "user"
    assert events[1]["type"] == "RUN_STARTED"


def test_v3_reconnect_uses_latest_run_not_a_stale_client_run_id() -> None:
    # The current pointer (server-elected) decides which run reconnect surfaces —
    # the latest, never an older one the client might still hold.
    store = _store()
    app, _ = _make_app(chat_stream_store=store, stream_idle_timeout=0.2)
    _seed_running_run(store, run="run_1", user_text="first question")
    _seed_running_run(store, run="run_2", user_text="second question")  # set_current → run_2

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert _events(r.text)[0]["content"] == "second question"


def test_v3_repeated_run_id_still_hits_upstream_each_time() -> None:
    # The buffer keys on a server-minted stream id, never the client run_id, so a
    # repeated run_id is NOT deduplicated — each POST reaches upstream and produces
    # a fresh run (v3 never deduped on run_id).
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.side_effect = [
        _resp_mock([_msg_line("first", message_id="m1"), _done_line()]),
        _resp_mock([_msg_line("second", message_id="m2"), _done_line()]),
    ]

    with TestClient(app) as client:
        first = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
        second = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert http_mock.send.call_count == 2  # both reached upstream, no replay
    deltas = lambda r: [e["delta"] for e in _events(r.text) if e["type"] == "TEXT_MESSAGE_CONTENT"]  # noqa: E731
    assert deltas(first) == ["first"]
    assert deltas(second) == ["second"]


def test_v3_resume_turn_does_not_stash_user_input() -> None:
    # A HITL resume turn carries no new question — it must not stash a user turn,
    # else reconnect would replay the previous question as a new one.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("resumed", message_id="m1"), _done_line()])

    body = _run_input()
    body["resume"] = [{"interruptId": "int-1", "status": "resolved"}]

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    stream_id = store.get_current("alice", "thread_1")
    assert store.get_user_input(ChatStreamStore.key("alice", "thread_1", stream_id)) is None


def test_v3_reconnect_replays_user_turn_for_zero_cursor() -> None:
    # "0" is a truthy from-start sentinel; the user turn must still be replayed.
    store = _store()
    app, _ = _make_app(chat_stream_store=store, stream_idle_timeout=0.2)
    _seed_running_run(store)

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice", "Last-Event-ID": "0"},
        )

    assert _events(r.text)[0]["type"] == "USER_MESSAGE"


def test_v3_reconnect_unknown_thread_emits_stream_expired() -> None:
    store = _store()
    app, _ = _make_app(chat_stream_store=store)

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_x"},
            headers={"X-User-Id": "alice"},
        )

    events = _events(r.text)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_STREAM_EXPIRED


def test_v3_reconnect_rejects_malformed_last_event_id() -> None:
    # A garbage Last-Event-ID would make the XRANGE cursor raise; the route must
    # reject it cleanly (RUN_ERROR over 200), never 500.
    store = _store()
    app, _ = _make_app(chat_stream_store=store)
    store.set_current("alice", "thread_1", "run_1")
    store.try_start(ChatStreamStore.key("alice", "thread_1", "run_1"))  # run is live

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice", "Last-Event-ID": "not-a-redis-id"},
        )

    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_STREAM_EXPIRED


def test_v3_reconnect_resumable_while_producer_lock_held_before_first_frame() -> None:
    # Startup race: lock taken, no frames yet — reconnect must NOT say expired.
    store = _store()
    app, _ = _make_app(chat_stream_store=store, stream_idle_timeout=0.2)
    store.set_current("alice", "thread_1", "run_1")
    store.try_start(ChatStreamStore.key("alice", "thread_1", "run_1"))

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    # No frames arrive, so the stream idle-times-out empty — but crucially it did
    # not short-circuit to STREAM_EXPIRED.
    assert r.status_code == 200
    assert HttpErrorCode.CHATAGENT_STREAM_EXPIRED not in r.text


def test_v3_post_falls_back_to_legacy_stream_when_store_unavailable() -> None:
    # try_start returning None (Redis down) must not break v3 chat — serve the
    # legacy connection-bound stream instead.
    store = MagicMock(spec=ChatStreamStore)
    store.key.return_value = "chatstream:alice:thread_1:run_1"
    store.try_start.return_value = None
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    types = [e["type"] for e in _events(r.text)]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    store.read_after.assert_not_called()  # never entered the buffered consumer


def test_v3_reconnect_is_owner_scoped() -> None:
    # A different user cannot reconnect to alice's run: the current pointer is
    # per-user, so mallory resolves no run on the same thread.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "mallory"},
        )

    events = _events(r.text)
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_STREAM_EXPIRED


# --- T-CAv3L: session-list live status (running spinner + new-reply dot) -------


def test_v3_sessionlist_enriches_running_and_has_new_reply() -> None:
    # thread_1 has an in-flight run (spinner); thread_2 has a completed-but-unopened
    # reply (dot). The list reflects both from the store.
    store = _store()
    _seed_running_run(store, thread="thread_1", run="run_1")
    store.mark_unread("alice", "thread_2")
    app, http_mock = _make_app(chat_stream_store=store, sessionlist_url="http://up/list")
    http_mock.get.return_value = _json_resp(
        {
            "totalCount": 2,
            "sessions": [
                {"session": "thread_1", "sessionName": "A"},
                {"session": "thread_2", "sessionName": "B"},
            ],
        }
    )

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/sessionList", headers={"X-User-Id": "alice"})

    by_id = {s["session"]: s for s in r.json()["sessions"]}
    assert by_id["thread_1"]["running"] is True
    assert by_id["thread_1"]["hasNewReply"] is False
    assert by_id["thread_2"]["running"] is False
    assert by_id["thread_2"]["hasNewReply"] is True


def test_v3_sessionlist_without_store_omits_status_fields() -> None:
    # No store wired → the list degrades to title-only (pre-T-CAv3L shape).
    app, http_mock = _make_app(sessionlist_url="http://up/list")
    http_mock.get.return_value = _json_resp(
        {"totalCount": 1, "sessions": [{"session": "t1", "sessionName": "A"}]}
    )

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/sessionList", headers={"X-User-Id": "alice"})

    entry = r.json()["sessions"][0]
    assert "running" not in entry
    assert "hasNewReply" not in entry


def test_v3_get_session_does_not_clear_unread() -> None:
    # Read is now an explicit client signal (POST /session/read). Loading history
    # via GET /session is decoupled from "read" and must NOT drop the dot.
    store = _store()
    store.mark_unread("alice", "thread_1")
    app, http_mock = _make_app(chat_stream_store=store, session_url="http://up/session")
    http_mock.get.return_value = _json_resp(
        {"session": "thread_1", "sessionName": "X", "messages": []}
    )

    with TestClient(app) as client:
        client.get(
            "/chatagent/v3/session",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert store.has_unread("alice", "thread_1") is True  # loading history ≠ read


def test_v3_session_read_endpoint_clears_and_publishes() -> None:
    # The explicit client-owned mark-read: POST /session/read clears the unread flag
    # and publishes the cleared dot over NATS. This is now the ONLY path to "read".
    store = _store()
    store.mark_unread("alice", "thread_1")
    pub = MagicMock(spec=NatsSessionPublisher)
    app, _ = _make_app(chat_stream_store=store, nats_publisher=pub)

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v3/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 204
    assert store.has_unread("alice", "thread_1") is False
    assert any(
        call.args[1] == {"session": "thread_1", "hasNewReply": False}
        for call in pub.publish.call_args_list
    )


def test_v3_session_read_skips_broadcast_when_already_read() -> None:
    # Repeat mark-reads are silent: the FE calls this on every session view, so an
    # unconditional publish would spam a no-op cleared-dot delta each time. Only an
    # actual flag deletion broadcasts.
    store = _store()  # no unread flag set
    pub = MagicMock(spec=NatsSessionPublisher)
    app, _ = _make_app(chat_stream_store=store, nats_publisher=pub)

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v3/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 204
    pub.publish.assert_not_called()


def test_v3_session_read_is_noop_204_without_store() -> None:
    # No stream store wired (the whole unread feature is off) → the endpoint is a
    # harmless no-op 204 and never publishes.
    pub = MagicMock(spec=NatsSessionPublisher)
    app, _ = _make_app(nats_publisher=pub)  # chat_stream_store defaults to None

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v3/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 204
    pub.publish.assert_not_called()


def test_v3_active_viewer_keeps_unread_after_completion() -> None:
    # The consumer no longer clears on eos: watching the reply stream to the end is
    # NOT "read" (read is an explicit POST /session/read). The producer's dot
    # persists until the client explicitly marks the session read.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert store.has_unread("alice", "thread_1") is True  # eos drain no longer clears


class _FinishedAgent:
    def run(self, request, model):  # noqa: ARG002 — stub matches the Agent protocol
        yield to_sse(RunStartedEvent(run_id="r", thread_id="t"))
        yield to_sse(RunFinishedEvent(run_id="r", thread_id="t"))


class _ErroredAgent:
    def run(self, request, model):  # noqa: ARG002 — terminal is RUN_ERROR (no reply persisted)
        yield to_sse(RunStartedEvent(run_id="r", thread_id="t"))
        yield to_sse(RunErrorEvent(message="boom", run_id="r", thread_id="t"))


def test_run_producer_marks_unread_on_successful_finish() -> None:
    # The producer marks the thread unread (the background/no-viewer case — a watching
    # consumer would later clear it on eos drain).
    from ragent.routers.chatagent_v3 import _run_producer

    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    _run_producer(
        store, None, key, _FinishedAgent(), object(), "", "alice", "t", reply_expected=True
    )
    assert store.has_unread("alice", "t") is True


def test_run_producer_skips_unread_for_control_only_resume() -> None:
    # A cancelled-only resume yields no new reply → no dot.
    from ragent.routers.chatagent_v3 import _run_producer

    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    _run_producer(
        store, None, key, _FinishedAgent(), object(), "", "alice", "t", reply_expected=False
    )
    assert store.has_unread("alice", "t") is False


def test_run_producer_skips_unread_when_run_errors() -> None:
    # A run ending in RUN_ERROR (e.g. upstream timeout after disconnect) persisted no
    # reply — it must not dot the session even though reply_expected is True.
    from ragent.routers.chatagent_v3 import _run_producer

    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    _run_producer(
        store, None, key, _ErroredAgent(), object(), "", "alice", "t", reply_expected=True
    )
    assert store.has_unread("alice", "t") is False


def test_run_producer_publishes_running_transitions_over_nats() -> None:
    from ragent.routers.chatagent_v3 import _run_producer

    pub = MagicMock(spec=NatsSessionPublisher)
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    _run_producer(
        store, pub, key, _FinishedAgent(), object(), "", "alice", "t", reply_expected=True
    )

    events = [call.args[1] for call in pub.publish.call_args_list]
    assert {"session": "t", "running": True} in events
    assert {"session": "t", "running": False, "hasNewReply": True} in events


def test_run_producer_omits_has_new_reply_when_no_reply_was_produced() -> None:
    # A no-reply finish (RUN_ERROR here) never touched the unread flag, so its delta
    # must omit hasNewReply entirely: an absolute false would wrongly wipe a live
    # subscriber's existing dot (an earlier still-unread reply) until the next
    # snapshot re-sync — clearing is client-owned (POST /session/read) now.
    from ragent.routers.chatagent_v3 import _run_producer

    pub = MagicMock(spec=NatsSessionPublisher)
    store = _store()
    store.mark_unread("alice", "t")  # dot from an earlier, still-unread reply
    key = ChatStreamStore.key("alice", "t", "r")
    _run_producer(store, pub, key, _ErroredAgent(), object(), "", "alice", "t", reply_expected=True)

    events = [call.args[1] for call in pub.publish.call_args_list]
    assert {"session": "t", "running": False} in events  # spinner still cleared
    assert not any(e.get("hasNewReply") is False for e in events)
    assert store.has_unread("alice", "t") is True  # flag untouched by the errored run


def test_run_producer_logs_instead_of_swallowing_a_background_error() -> None:
    # Fire-and-forget: an escaping error (e.g. mark_done on a Redis drop) must be logged,
    # not silently swallowed by the executor Future.
    from structlog.testing import capture_logs

    from ragent.routers.chatagent_v3 import _run_producer

    store = MagicMock(spec=ChatStreamStore)
    store.mark_done.side_effect = RuntimeError("redis dropped")

    with capture_logs() as logs:
        _run_producer(
            store, None, "k", _FinishedAgent(), object(), "", "alice", "t", reply_expected=True
        )

    assert any(e["event"] == "chatagent_v3.producer_failed" for e in logs)


def test_consume_stream_does_not_clear_unread_on_eos() -> None:
    # Reaching eos only STOPS the stream — it no longer marks read. "Read" is a
    # client-owned signal (POST /session/read), so the dot must not depend on this
    # generator's lifecycle (whether a client streamed to the end or disconnected).
    from ragent.routers.chatagent_v3 import _consume_stream

    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    store.mark_unread("alice", "t")
    store.append(key, "data: a\n\n")
    store.mark_done(key)  # eos present

    frames = list(_consume_stream(store, key, "0", 1.0, 0.01))

    assert len(frames) == 1  # streamed the one buffered frame, then stopped at eos
    assert store.has_unread("alice", "t") is True  # eos drain does NOT clear


def test_v3_router_does_not_import_concrete_agent_or_caller_classes() -> None:
    # SOLID/DIP regression: the router must depend only on the injected
    # `agent_factory` callable (typed against the twp_ai.agent.Agent Protocol),
    # never on a concrete Agent/Caller implementation. Swapping the upstream
    # brain must not require touching this module.
    import ragent.routers.chatagent_v3 as router_module

    source = inspect.getsource(router_module)
    assert "ADKAgent" not in source
    assert "ADKCaller" not in source


def test_v3_post_uses_injected_agent_factory_not_a_hardcoded_backend() -> None:
    # A stub Agent satisfying only the Protocol must work — proving the
    # router never constructs its own caller/agent internally.
    calls: list[tuple[str, str]] = []

    class _StubAgent:
        def run(self, request, model):
            calls.append((request.thread_id or "", model))
            yield _done_line()

    def _factory(user_id: str, user_token: str, attachments: str | None = None):
        return _StubAgent()

    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_chatagent_v3_router(
            http_client=http_mock,
            chatagent_ap_name="TestAP",
            chatagent_auth="Bearer up",
            chatagent_api_url="http://upstream",
            agent_factory=_factory,
        )
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert calls  # the injected stub agent's run() was actually invoked
    http_mock.send.assert_not_called()  # the router itself never talks to ADK


def test_v3_router_builds_without_agent_factory_when_post_route_disabled() -> None:
    # Session-only deployment: CHATAGENT_API_URL unset (no POST route) but
    # CHATAGENT_SESSION_API_URL set. agent_factory is never built in that case
    # (composition.py only builds it when chatagent_api_url is set), so the
    # router must not require it.
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_chatagent_v3_router(
            http_client=http_mock,
            chatagent_ap_name="TestAP",
            chatagent_session_api_url="http://upstream/session",
        )
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 404  # POST route not registered, no crash building the app
