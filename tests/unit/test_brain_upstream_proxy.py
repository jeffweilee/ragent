"""T-BRAIN.6 — generic authenticated reverse proxy over brain's /upstream/*."""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.brain_upstream_proxy import create_brain_upstream_proxy_router


def _make_app(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    app = FastAPI()
    app.include_router(
        create_brain_upstream_proxy_router(
            http_client=client, brain_url="http://brain:8100", brain_key="sekret", timeout=5.0
        )
    )
    return app


def test_forwards_to_upstream_with_service_headers_and_user_override_in_query() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    app = _make_app(handler)
    with TestClient(app) as client:
        # client forges user=evil in the query; the proxy must override it.
        r = client.get("/brainagent/v1/memory?user=evil", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert seen["url"] == "http://brain:8100/upstream/memory?user=alice"
    assert seen["headers"]["x-user-id"] == "alice"
    assert seen["headers"]["x-brain-key"] == "sekret"


def test_user_override_in_json_body() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": "p1"})

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1/projects",
            json={"user": "evil", "name": "My Project"},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 201
    # forged body user overridden; other fields preserved.
    assert seen["body"] == {"user": "alice", "name": "My Project"}


def test_relays_422_i18n_envelope_verbatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "memory_too_long", "params": {"limit": 2000}})

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.put(
            "/brainagent/v1/memory/core",
            json={"block": "human", "content": "x"},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 422
    assert r.json() == {"error": "memory_too_long", "params": {"limit": 2000}}


def test_relays_binary_download_with_headers() -> None:
    blob = b"\x89PNG\r\n\x1a\nbinary-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=blob,
            headers={
                "content-type": "image/png",
                "content-disposition": 'attachment; filename="a.png"',
            },
        )

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.get("/brainagent/v1/artifacts/art-1", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.content == blob
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == 'attachment; filename="a.png"'


def test_timeout_maps_to_504() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.get("/brainagent/v1/skills", headers={"X-User-Id": "alice"})
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.BRAINAGENT_TIMEOUT


def test_connection_error_maps_to_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.get("/brainagent/v1/skills", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR


def test_denies_admin_reindex_without_reaching_upstream() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"ok": True})

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.post("/brainagent/v1/reindex", headers={"X-User-Id": "alice"})
    assert r.status_code == 404
    assert called["n"] == 0  # never reached brain's admin endpoint


def test_preserves_multi_value_query_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["tags"] = request.url.params.get_list("tag")
        seen["user"] = request.url.params.get("user")
        return httpx.Response(200, json={"ok": True})

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.get("/brainagent/v1/memory?tag=a&tag=b", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert seen["tags"] == ["a", "b"]  # both values preserved
    assert seen["user"] == "alice"


def test_forwards_content_type_for_raw_body() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.read()
        return httpx.Response(201, json={"ok": True})

    app = _make_app(handler)
    with TestClient(app) as client:
        r = client.post(
            "/brainagent/v1/artifacts",
            content=b"col1,col2\n1,2\n",
            headers={"X-User-Id": "alice", "Content-Type": "text/csv"},
        )
    assert r.status_code == 201
    assert seen["content_type"] == "text/csv"
    assert seen["body"] == b"col1,col2\n1,2\n"


def test_forwards_accept_header() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    app = _make_app(handler)
    with TestClient(app) as client:
        client.get(
            "/brainagent/v1/artifacts/a1",
            headers={"X-User-Id": "alice", "Accept": "image/png"},
        )
    assert seen["accept"] == "image/png"
