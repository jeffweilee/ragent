"""T-CAv3.4 — chatagent v3 router (twp-ai protocol proxy) unit tests."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import fakeredis
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.rate_limiter import RateLimiter, RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router
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
    stream_idle_timeout: float = 3.0,
):
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    router = create_chatagent_v3_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_auth="Bearer up",
        chatagent_api_url="http://upstream",
        agent_factory=_real_agent_factory(
            http_mock, api_url="http://upstream", ap_name="TestAP", auth="Bearer up"
        ),
        rate_limiter=rate_limiter,
        chat_stream_store=chat_stream_store,
        stream_idle_timeout=stream_idle_timeout,
    )
    app.include_router(router)
    return app, http_mock


def _store() -> ChatStreamStore:
    return ChatStreamStore(fakeredis.FakeStrictRedis(decode_responses=True))


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


def test_v3_reconnect_resumes_after_last_event_id() -> None:
    # Simulate a drop after the first content frame, then reconnect with that
    # frame's id: only the strictly-later frames replay.
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
        first = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
        # The producer has finished and the buffer is retained within its TTL.
        ids = _ids(first.text)
        resume_from = ids[1]  # after RUN_STARTED + first frame boundary

        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice", "Last-Event-ID": resume_from},
        )

    replayed = _events(r.text)
    # Everything up to and including `resume_from` is excluded; an incremental
    # resume (Last-Event-ID present) does NOT re-emit the user turn.
    assert "RUN_STARTED" not in [e["type"] for e in replayed]
    assert "USER_MESSAGE" not in [e["type"] for e in replayed]
    assert replayed[-1]["type"] == "RUN_FINISHED"
    resumed_ids = _ids(r.text)
    assert all(i > resume_from for i in resumed_ids)


def test_v3_reconnect_resolves_current_run_and_replays_user_turn() -> None:
    # From-start reconnect needs only thread_id; the server resolves the current
    # run and prepends the stashed user turn (the live stream never carried it).
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
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
    assert events[-1]["type"] == "RUN_FINISHED"


def test_v3_reconnect_uses_latest_run_not_a_stale_client_run_id() -> None:
    # Two runs on the same thread; reconnect must surface the LATEST (run_2),
    # never resurrect the older already-finished run_1 — the client never gets to
    # pin run_id, the server's current pointer decides.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    body2 = _run_input()
    body2["runId"] = "run_2"
    body2["messages"] = [{"id": "m2", "role": "user", "content": "second question"}]

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
        client.post("/chatagent/v3", json=body2, headers={"X-User-Id": "alice"})
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    events = _events(r.text)
    assert events[0]["type"] == "USER_MESSAGE"
    assert events[0]["content"] == "second question"  # latest run, not "the features"


def test_v3_stale_retry_does_not_rewind_current_pointer() -> None:
    # A retried POST for an OLDER run (lock already held → not elected) must not
    # rewind the thread's current pointer back to it.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    body2 = _run_input()
    body2["runId"] = "run_2"
    body2["messages"] = [{"id": "m2", "role": "user", "content": "second question"}]

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})  # run_1
        client.post("/chatagent/v3", json=body2, headers={"X-User-Id": "alice"})  # run_2
        client.post(
            "/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"}
        )  # stale run_1
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    assert store.get_current("alice", "thread_1") == "run_2"
    assert _events(r.text)[0]["content"] == "second question"


def test_v3_reconnect_omits_user_message_for_hitl_resume_turn() -> None:
    # A HITL resume turn carries no new question — reconnect must not replay the
    # last historical user message as a USER_MESSAGE.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("resumed", message_id="m1"), _done_line()])

    body = _run_input()
    body["resume"] = [{"interruptId": "int-1", "status": "resolved"}]

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})
        r = client.get(
            "/chatagent/v3/reconnect",
            params={"thread_id": "thread_1"},
            headers={"X-User-Id": "alice"},
        )

    types = [e["type"] for e in _events(r.text)]
    assert "USER_MESSAGE" not in types
    assert types[0] == "RUN_STARTED"


def test_v3_reconnect_replays_user_turn_for_zero_cursor() -> None:
    # "0" is a truthy from-start sentinel; the user turn must still be replayed.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
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


def test_v3_duplicate_run_id_spawns_single_producer() -> None:
    # The SET NX lock means a retried POST with the same runId reuses the buffer
    # instead of generating again — the upstream is called exactly once.
    store = _store()
    app, http_mock = _make_app(chat_stream_store=store)
    http_mock.send.return_value = _resp_mock([_msg_line("hi", message_id="m1"), _done_line()])

    with TestClient(app) as client:
        first = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})
        second = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert http_mock.send.call_count == 1
    # Both responses replay the same buffered run.
    assert [e["type"] for e in _events(first.text)] == [e["type"] for e in _events(second.text)]


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

    def _factory(user_id: str, user_token: str):
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
