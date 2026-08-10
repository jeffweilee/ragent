"""T-BRAIN.4/5 — /brainagent/v1 router (passthrough run, reconnect, cancel)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import fakeredis
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twp_ai.events import RunFinishedEvent, RunStartedEvent, to_sse
from twp_ai.schemas import RunAgentInput

from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.nats_publisher import NatsSessionPublisher
from ragent.clients.rate_limiter import RateLimiter, RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.brainagent import create_brainagent_v1_router
from tests.helpers import parse_sse_events as _events


class _EchoAgent:
    """Stands in for BrainAgent — echoes brain's native twp-ai envelope."""

    def run(self, body: RunAgentInput, model: str) -> Generator[str, None, None]:
        yield to_sse(RunStartedEvent(run_id=body.run_id, thread_id=body.thread_id))
        yield to_sse(RunFinishedEvent(run_id=body.run_id, thread_id=body.thread_id))


def _store() -> ChatStreamStore:
    return ChatStreamStore(fakeredis.FakeStrictRedis(decode_responses=True))


def _make_app(
    *,
    rate_limiter: RateLimiter | None = None,
    chat_stream_store: ChatStreamStore | None = None,
    nats_publisher: NatsSessionPublisher | None = None,
    http_client: httpx.Client | None = None,
):
    http_mock = http_client or MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_brainagent_v1_router(
            http_client=http_mock,
            brain_url="http://brain:8100",
            brain_key="sekret",
            agent_factory=lambda user_id, extra_headers=None: _EchoAgent(),
            rate_limiter=rate_limiter,
            chat_stream_store=chat_stream_store,
            nats_publisher=nats_publisher,
            stream_idle_timeout=3.0,
        )
    )
    return app, http_mock


def _run_input(*, thread_id: str | None = "thread_1") -> dict:
    body: dict = {
        "runId": "run_1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": None,
    }
    if thread_id is not None:
        body["threadId"] = thread_id
    return body


def test_relays_brain_envelope() -> None:
    app, _ = _make_app(chat_stream_store=_store())
    with TestClient(app) as client:
        r = client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "alice"})
    types = [e["type"] for e in _events(r.text)]
    assert types == ["RUN_STARTED", "RUN_FINISHED"]


def test_mints_thread_id_when_omitted() -> None:
    app, _ = _make_app(chat_stream_store=_store())
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1", json=_run_input(thread_id=None), headers={"X-User-Id": "alice"}
        )
    started = next(e for e in _events(r.text) if e["type"] == "RUN_STARTED")
    assert started["threadId"]  # minted, non-null


def test_rate_limited_yields_run_error() -> None:
    limiter = MagicMock(spec=RateLimiter)
    result = MagicMock(spec=RateLimitResult)
    result.allowed = False
    limiter.check.return_value = result
    app, _ = _make_app(rate_limiter=limiter, chat_stream_store=_store())
    with TestClient(app) as client:
        r = client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "dave"})
    events = _events(r.text)
    assert events[0]["type"] == "RUN_ERROR"
    assert events[0]["code"] == HttpErrorCode.BRAINAGENT_RATE_LIMITED
    assert limiter.check.call_args.args[0] == "brainagent:dave"


def test_reconnect_expired_when_no_current_run() -> None:
    app, _ = _make_app(chat_stream_store=_store())
    with TestClient(app) as client:
        r = client.get("/brainagent/v1/reconnect?thread_id=nope", headers={"X-User-Id": "alice"})
    events = _events(r.text)
    assert events[0]["type"] == "RUN_ERROR"
    assert events[0]["code"] == HttpErrorCode.CHATAGENT_STREAM_EXPIRED


def test_cancel_proxies_to_brain_with_owner_headers() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.content = b'{"cancelled": true}'
    resp.json.return_value = {"cancelled": True}
    http_mock.post.return_value = resp
    app, _ = _make_app(http_client=http_mock)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1/runs/run_9/cancel", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}
    url = http_mock.post.call_args.args[0]
    headers = http_mock.post.call_args.kwargs["headers"]
    assert url == "http://brain:8100/runs/run_9/cancel"
    assert headers["X-User-Id"] == "alice"
    assert headers["X-Brain-Key"] == "sekret"


def test_cancel_survives_non_json_upstream_response() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 502
    resp.content = b"<html>502 Bad Gateway</html>"
    resp.json.side_effect = ValueError("not json")
    http_mock.post.return_value = resp
    app, _ = _make_app(http_client=http_mock)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1/runs/x/cancel", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json() == {"cancelled": False}


def test_cancel_relays_404() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 404
    resp.content = b'{"cancelled": false}'
    resp.json.return_value = {"cancelled": False}
    http_mock.post.return_value = resp
    app, _ = _make_app(http_client=http_mock)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1/runs/x/cancel", headers={"X-User-Id": "alice"})
    assert r.status_code == 404


def test_session_read_clears_unread_and_publishes_locally() -> None:
    # Mark-read is ragent-owned (read/unread lives in ragent's Redis, not brain).
    # It must be handled locally with a 204 — never proxied upstream, which would
    # 404 at brain (which has no /upstream/session/read route).
    store = _store()
    store.mark_unread("alice", "thread_1")
    pub = MagicMock(spec=NatsSessionPublisher)
    http_mock = MagicMock(spec=httpx.Client)
    app, _ = _make_app(chat_stream_store=store, nats_publisher=pub, http_client=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 204
    assert store.has_unread("alice", "thread_1") is False
    pub.publish.assert_called_once_with("alice", {"session": "thread_1", "hasNewReply": False})
    http_mock.request.assert_not_called()  # never leaked to the brain proxy


def test_session_read_skips_broadcast_when_already_read() -> None:
    # Repeat mark-reads are silent: only an actual flag deletion broadcasts.
    store = _store()  # no unread flag set
    pub = MagicMock(spec=NatsSessionPublisher)
    app, _ = _make_app(chat_stream_store=store, nats_publisher=pub)
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 204
    pub.publish.assert_not_called()


def test_session_read_is_noop_204_without_store() -> None:
    # No stream store wired (unread feature off) → harmless local no-op 204. The
    # route stays registered so it never leaks to the proxy and 404s at brain.
    http_mock = MagicMock(spec=httpx.Client)
    app, _ = _make_app(chat_stream_store=None, http_client=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1/session/read",
            params={"session": "thread_1"},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 204
    http_mock.request.assert_not_called()  # never leaked to the brain proxy


# --- T-PAT.29: PAT attach on the RUN path only ------------------------------

from ragent.auth.deps import get_forwarded_headers  # noqa: E402


class _StubPat:
    def __init__(self, *, token=None):
        self._token = token

    async def resolve_best_effort(self, nt: str):
        return self._token


def _pat_app(*, pat_service, http_client=None, capture=None, header_name="pat"):
    def factory(user_id, extra_headers=None):
        if capture is not None:
            capture["extra"] = extra_headers
        return _EchoAgent()

    app = FastAPI()
    app.include_router(
        create_brainagent_v1_router(
            http_client=http_client or MagicMock(spec=httpx.Client),
            brain_url="http://brain:8100",
            brain_key="sekret",
            agent_factory=factory,
            pat_service=pat_service,
            pat_header_name=header_name,
        )
    )
    return app


def test_run_path_attaches_resolved_pat() -> None:
    capture: dict = {}
    app = _pat_app(pat_service=_StubPat(token="SERVER-PAT"), capture=capture)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert capture["extra"]["pat"] == "SERVER-PAT"  # PAT rode into BrainCaller's extra_headers


def test_run_path_fail_open_without_pat() -> None:
    capture: dict = {}
    app = _pat_app(pat_service=None, capture=capture)  # PAT slice off
    with TestClient(app) as client:
        client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "alice"})
    assert "pat" not in (capture["extra"] or {})


def test_run_path_resolved_pat_wins_over_forwarded() -> None:
    capture: dict = {}
    app = _pat_app(pat_service=_StubPat(token="SERVER-PAT"), capture=capture)
    app.dependency_overrides[get_forwarded_headers] = lambda: {"pat": "FORGED"}
    with TestClient(app) as client:
        client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "alice"})
    assert capture["extra"]["pat"] == "SERVER-PAT"  # resolved wins over the forwarded value


def test_run_path_pat_header_colliding_with_a_service_header_is_not_attached() -> None:
    # An operator misconfigures PAT_UPSTREAM_HEADER_NAME as a service-owned
    # header; the PAT must not overwrite the caller identity brain scopes by.
    capture: dict = {}
    app = _pat_app(pat_service=_StubPat(token="PAT-EVIL"), capture=capture, header_name="X-User-Id")
    with TestClient(app) as client:
        r = client.post("/brainagent/v1", json=_run_input(), headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert "X-User-Id" not in (capture["extra"] or {})


def test_cancel_path_attaches_no_pat() -> None:
    # Cancelling a run is brain-internal bookkeeping — it invokes no drive tool,
    # so it needs no PAT and must not pay a side-effecting resolve.
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["pat"] = request.headers.get("pat")
        return httpx.Response(200, json={"cancelled": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    app = _pat_app(pat_service=_StubPat(token="SERVER-PAT"), http_client=http)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1/runs/run_1/cancel", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert seen["pat"] is None
