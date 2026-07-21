"""T-PAT.6 — PatRefreshClient request shape + typed error mapping."""

from __future__ import annotations

import httpx
import pytest

from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshClient,
    PatRefreshRateLimited,
    PatRefreshTransient,
    PatRefreshUnauthorized,
)

_URL = "https://pat.example/refresh"


def _client(handler) -> PatRefreshClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PatRefreshClient(
        http,
        refresh_url=_URL,
        header_key="X-Pat-Service-Token",
        header_value="svc-secret",
        timeout=5.0,
    )


def test_success_returns_new_token_and_sends_expected_shape() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("X-Pat-Service-Token")
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"patToken": "NEW"})

    assert _client(handler).refresh("OLD") == "NEW"
    assert seen["method"] == "PUT"
    assert seen["url"] == _URL
    assert seen["header"] == "svc-secret"
    assert seen["body"] == {"patToken": "OLD"}


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, PatRefreshUnauthorized),
        (400, PatRefreshBadRequest),
        (429, PatRefreshRateLimited),
        (500, PatRefreshTransient),
        (503, PatRefreshTransient),
    ],
)
def test_status_maps_to_typed_error(status: int, exc: type[Exception]) -> None:
    client = _client(lambda req: httpx.Response(status, json={}))
    with pytest.raises(exc):
        client.refresh("OLD")


def test_transport_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(PatRefreshTransient):
        _client(handler).refresh("OLD")


def test_malformed_200_body_is_transient() -> None:
    client = _client(lambda req: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(PatRefreshTransient):
        client.refresh("OLD")
