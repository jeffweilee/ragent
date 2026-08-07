"""T-PAT.22 — /pat/v1/authorize router: identity, id-token verification, errors.

The caller sends TWO tokens: the access token the auth middleware already
verified (identity, via `Depends(get_user_id)`) and an SSO **id token** on the
fixed `X-Id-Token` header, which ragent verifies itself before forwarding it to
the PAT init service. The id token is verified with the same JWKS/issuer/audience
as the access token, and its username claim must equal the resolved caller —
otherwise a caller could mint a PAT on someone else's behalf.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.routers.pat import ID_TOKEN_HEADER, create_pat_router
from ragent.services.pat_service import (
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
    PatReauthRequired,
)
from tests.unit.pat_fakes import JWT_CLAIM_USER_ID, id_token_manager, sign_id_token


class _StubService:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

        self.revoked: list[str] = []

    async def authorize(self, *, nt: str, id_token: str) -> None:
        self.calls.append((nt, id_token))
        if self.raise_exc is not None:
            raise self.raise_exc

    async def revoke(self, *, nt: str) -> None:
        self.revoked.append(nt)


def _client(service: _StubService) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_pat_router(
            pat_service=service,
            token_manager=id_token_manager(),
            jwt_claim_user_id=JWT_CLAIM_USER_ID,
        )
    )
    return TestClient(app)


def test_authorize_success_forwards_the_verified_id_token() -> None:
    service = _StubService()
    id_token = sign_id_token("alice")

    resp = _client(service).post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", ID_TOKEN_HEADER: id_token},
    )

    assert resp.status_code == 204
    # nt from the identity header; the id token forwarded verbatim — never a body field.
    assert service.calls == [("alice", id_token)]


def test_authorize_without_identity_returns_422() -> None:
    service = _StubService()

    resp = _client(service).post(
        "/pat/v1/authorize", headers={ID_TOKEN_HEADER: sign_id_token("alice")}
    )

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MISSING_USER_ID"
    assert service.calls == []


def test_authorize_without_id_token_returns_401() -> None:
    service = _StubService()

    resp = _client(service).post("/pat/v1/authorize", headers={"X-User-Id": "alice"})

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "PAT_REAUTH_REQUIRED"
    assert service.calls == []


@pytest.mark.parametrize(
    "bad_token,why",
    [
        ("not.a.jwt", "malformed"),
        (sign_id_token("alice", exp_delta=-60), "expired"),
        (sign_id_token("alice", aud="some-other-client"), "wrong audience"),
        (sign_id_token("alice", iss="https://evil.example"), "wrong issuer"),
        (sign_id_token("alice", claim="sub"), "username claim missing"),
    ],
)
def test_authorize_rejects_an_unverifiable_id_token(bad_token: str, why: str) -> None:
    """A bad id token is rejected at OUR edge — the init service is never called,
    so a junk token cannot burn the 10/60 s init rate-limit budget."""
    service = _StubService()

    resp = _client(service).post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", ID_TOKEN_HEADER: bad_token},
    )

    assert resp.status_code == 401, why
    assert resp.json()["error_code"] == "PAT_REAUTH_REQUIRED"
    assert service.calls == []


def test_authorize_rejects_an_id_token_belonging_to_another_user() -> None:
    """The anti-impersonation check: a perfectly valid id token for bob must not
    let alice mint a PAT (nor reach init with someone else's credential)."""
    service = _StubService()

    resp = _client(service).post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", ID_TOKEN_HEADER: sign_id_token("bob")},
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "PAT_REAUTH_REQUIRED"
    assert service.calls == []


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
    resp = _client(_StubService(raise_exc=exc)).post(
        "/pat/v1/authorize",
        headers={"X-User-Id": "alice", ID_TOKEN_HEADER: sign_id_token("alice")},
    )

    assert resp.status_code == status
    assert resp.json()["error_code"] == code


def test_id_token_header_is_published_in_the_openapi_schema() -> None:
    """FastAPI documents the header natively, so Swagger and generated clients
    send it without any endpoint-specific security-scheme special case."""
    app = FastAPI()
    app.include_router(
        create_pat_router(
            pat_service=_StubService(),
            token_manager=id_token_manager(),
            jwt_claim_user_id=JWT_CLAIM_USER_ID,
        )
    )

    params = app.openapi()["paths"]["/pat/v1/authorize"]["post"]["parameters"]
    assert [p["name"] for p in params if p["in"] == "header"] == [ID_TOKEN_HEADER]


@pytest.mark.parametrize(
    "headers,reason,error_code",
    [
        ({ID_TOKEN_HEADER: sign_id_token("alice")}, "missing_user_id", "MISSING_USER_ID"),
        ({"X-User-Id": "alice"}, "missing_id_token", "PAT_REAUTH_REQUIRED"),
        (
            {"X-User-Id": "alice", ID_TOKEN_HEADER: sign_id_token("alice", exp_delta=-60)},
            "id_token_invalid",
            "AUTH_TOKEN_EXPIRED",
        ),
        (
            {"X-User-Id": "alice", ID_TOKEN_HEADER: sign_id_token("bob")},
            "id_token_owner_mismatch",
            "PAT_REAUTH_REQUIRED",
        ),
    ],
)
def test_every_rejection_is_logged_with_its_reason(
    headers: dict, reason: str, error_code: str
) -> None:
    """The response is deliberately vague, so the log is the only place an
    operator can tell these four apart (00_rule.md §Service Boundary Logs)."""
    with capture_logs() as captured:
        _client(_StubService()).post("/pat/v1/authorize", headers=headers)

    rejections = [e for e in captured if e.get("event") == "pat.authorize.rejected"]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == reason
    assert rejections[0]["error_code"] == error_code
    assert rejections[0]["log_level"] == "warning"


# --- DELETE /pat/v1/authorize (T-PAT.26) ---------------------------------


def test_revoke_returns_204_and_calls_the_service() -> None:
    service = _StubService()

    resp = _client(service).delete("/pat/v1/authorize", headers={"X-User-Id": "alice"})

    assert resp.status_code == 204
    assert service.revoked == ["alice"]


def test_revoke_needs_no_id_token() -> None:
    # The upstream has no revoke endpoint, so nothing is called on-behalf-of the
    # user — requiring X-Id-Token here would be a gate with no purpose.
    service = _StubService()

    resp = _client(service).delete("/pat/v1/authorize", headers={"X-User-Id": "alice"})

    assert resp.status_code == 204


def test_revoke_is_idempotent_for_a_caller_who_never_authorized() -> None:
    # The PAT is a per-caller singleton, not an id-addressed object: revoking
    # states a target state, so repeating it is success, never 404.
    service = _StubService()
    client = _client(service)

    assert client.delete("/pat/v1/authorize", headers={"X-User-Id": "ghost"}).status_code == 204
    assert client.delete("/pat/v1/authorize", headers={"X-User-Id": "ghost"}).status_code == 204
    assert service.revoked == ["ghost", "ghost"]


def test_revoke_without_identity_returns_422_and_never_calls_the_service() -> None:
    service = _StubService()

    with capture_logs() as captured:
        resp = _client(service).delete("/pat/v1/authorize")

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MISSING_USER_ID"
    assert service.revoked == []
    rejected = [e for e in captured if e.get("event") == "pat.revoke.rejected"]
    assert len(rejected) == 1
    assert rejected[0]["error_code"] == "MISSING_USER_ID"
