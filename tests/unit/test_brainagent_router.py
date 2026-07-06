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
    http_client: httpx.Client | None = None,
):
    http_mock = http_client or MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_brainagent_v1_router(
            http_client=http_mock,
            brain_url="http://brain:8100",
            brain_key="sekret",
            agent_factory=lambda user_id: _EchoAgent(),
            rate_limiter=rate_limiter,
            chat_stream_store=chat_stream_store,
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
