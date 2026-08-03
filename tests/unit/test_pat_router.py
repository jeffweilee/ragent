"""T-PAT.18 — /pat/v1/authorize router: identity, id-token forward, typed error.

The FE no longer submits a `{patToken}`; ragent mints one via the init service
using the inbound SSO id token (read from the configured JWT header). The router
resolves nt from the identity header, forwards the id token to the service, and
maps the service's typed exceptions to problem-details responses.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.routers.pat import create_pat_router
from ragent.services.pat_service import (
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
    PatReauthRequired,
)

_ID_HEADER = "X-Auth-Token"


class _StubService:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    async def authorize(self, *, nt: str, id_token: str) -> None:
        self.calls.append((nt, id_token))
        if self.raise_exc is not None:
            raise self.raise_exc


def _client(service: _StubService) -> TestClient:
    app = FastAPI()
    app.include_router(create_pat_router(pat_service=service, id_token_header_name=_ID_HEADER))
    return TestClient(app)


def test_authorize_success_returns_204_and_forwards_identity_and_id_token() -> None:
    service = _StubService()
    client = _client(service)

    resp = client.post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", _ID_HEADER: "ID-TOKEN"},
    )

    assert resp.status_code == 204
    # nt from the identity header; id token from the JWT header — never a body field.
    assert service.calls == [("alice", "ID-TOKEN")]


def test_authorize_without_identity_returns_422() -> None:
    client = _client(_StubService())

    resp = client.post("/pat/v1/authorize", headers={_ID_HEADER: "ID-TOKEN"})

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_authorize_without_id_token_returns_401_reauth() -> None:
    service = _StubService()
    client = _client(service)

    resp = client.post("/pat/v1/authorize", headers={"X-User-Id": "alice"})

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "PAT_REAUTH_REQUIRED"
    assert service.calls == []  # never reached the service without an id token


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (PatTokenInvalid(), 401, "PAT_REAUTH_REQUIRED"),
        (PatReauthRequired(), 401, "PAT_REAUTH_REQUIRED"),
        (PatInitThrottled(), 429, "PAT_INIT_RATE_LIMITED"),
        (PatInitUnavailable(), 503, "PAT_INIT_UNAVAILABLE"),
        (PatInternalError("boom"), 500, "INTERNAL_ERROR"),
    ],
)
def test_authorize_maps_service_errors(exc: Exception, status: int, code: str) -> None:
    client = _client(_StubService(raise_exc=exc))

    resp = client.post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", _ID_HEADER: "ID-TOKEN"},
    )

    assert resp.status_code == status
    assert resp.json()["error_code"] == code
