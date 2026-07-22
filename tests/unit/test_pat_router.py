"""T-PAT.10 — /pat/v1/authorize router: identity, success, typed error."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.routers.pat import create_pat_router


class _StubService:
    def __init__(self, *, raise_invalid: bool = False) -> None:
        self.raise_invalid = raise_invalid
        self.calls: list[tuple[str, str]] = []

    async def authorize(self, *, nt: str, pat_token: str) -> None:
        self.calls.append((nt, pat_token))
        if self.raise_invalid:
            raise PatTokenInvalid()


def _client(service: _StubService) -> TestClient:
    app = FastAPI()
    app.include_router(create_pat_router(pat_service=service))
    return TestClient(app)


def test_authorize_success_returns_204_and_binds_header_identity() -> None:
    service = _StubService()
    client = _client(service)

    resp = client.post(
        "/pat/v1/authorize", json={"patToken": "PAT"}, headers={"X-User-Id": "alice"}
    )

    assert resp.status_code == 204
    assert service.calls == [("alice", "PAT")]  # nt from the header, not the body


def test_authorize_bad_token_returns_401_reauth() -> None:
    client = _client(_StubService(raise_invalid=True))

    resp = client.post(
        "/pat/v1/authorize", json={"patToken": "bad"}, headers={"X-User-Id": "alice"}
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "PAT_REAUTH_REQUIRED"


def test_authorize_without_identity_returns_422() -> None:
    client = _client(_StubService())

    resp = client.post("/pat/v1/authorize", json={"patToken": "PAT"})

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MISSING_USER_ID"


@pytest.mark.parametrize("body", [{}, {"patToken": ""}])
def test_authorize_missing_token_is_422(body: dict) -> None:
    client = _client(_StubService())
    resp = client.post("/pat/v1/authorize", json=body, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422
