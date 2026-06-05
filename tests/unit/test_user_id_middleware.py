"""User-id middleware: all four auth modes (T-AM.2 / §3.5)."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _PUBLIC_PATHS, _x_user_id_middleware
from ragent.bootstrap.auth_mode import AuthMode


def _build_app(
    *,
    auth_mode: AuthMode = AuthMode.user_header,
    user_id_header: str = "X-User-Id",
    jwt_header: str = "X-Auth-Token",
    jwt_claim: str = "preferred_username",
    token_manager=None,
) -> FastAPI:
    app = FastAPI()
    _x_user_id_middleware(
        app,
        auth_mode=auth_mode,
        user_id_header=user_id_header,
        jwt_header=jwt_header,
        jwt_claim=jwt_claim,
        token_manager=token_manager,
    )

    @app.get("/livez")
    def livez() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    def metrics() -> str:
        return "metrics"

    @app.get("/protected")
    def protected(
        x_user_id: str | None = Header(default=None, alias=user_id_header),
    ) -> dict:
        return {"user_id": x_user_id}

    return app


# ---------------------------------------------------------------- public paths


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/metrics"])
def test_probe_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_swagger_doc_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


def test_public_paths_includes_docs_and_probes() -> None:
    expected = {"/livez", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"}
    assert expected <= _PUBLIC_PATHS


# ---------------------------------------------------------------- none mode


def test_none_mode_injects_anonymous_without_header() -> None:
    app = _build_app(auth_mode=AuthMode.none)
    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "anonymous"


def test_none_mode_ignores_provided_header() -> None:
    app = _build_app(auth_mode=AuthMode.none)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-User-Id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "anonymous"


# ---------------------------------------------------------------- user_header mode


def test_user_header_mode_requires_header() -> None:
    app = _build_app(auth_mode=AuthMode.user_header)
    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_user_header_mode_passes_header_through() -> None:
    app = _build_app(auth_mode=AuthMode.user_header)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-User-Id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_custom_user_id_header_name() -> None:
    app = _build_app(auth_mode=AuthMode.user_header, user_id_header="X-Whoami")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Whoami": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


# ---------------------------------------------------------------- jwt_header mode


def test_jwt_header_mode_extracts_claim(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)
    token = make_token(preferred_username="alice")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_header_mode_ignores_user_id_header(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)
    token = make_token(preferred_username="alice")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token, "X-User-Id": "mallory"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_header_mode_missing_token_returns_401(oidc_token_manager) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)
    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_TOKEN_INVALID"


def test_jwt_header_mode_expired_returns_401(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)
    token = make_token(preferred_username="alice", exp=int(time.time()) - 1)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_TOKEN_EXPIRED"


def test_jwt_header_mode_missing_claim_returns_401(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)
    token = make_token()
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_CLAIM_MISSING"


def test_jwt_header_mode_without_token_manager_raises() -> None:
    with pytest.raises(RuntimeError, match="token_manager"):
        _build_app(auth_mode=AuthMode.jwt_header, token_manager=None)


def test_custom_jwt_header_and_claim(oidc_token_manager, make_token) -> None:
    app = _build_app(
        auth_mode=AuthMode.jwt_header,
        jwt_header="X-Token",
        jwt_claim="sub",
        token_manager=oidc_token_manager,
    )
    token = make_token(sub="carol")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Token": token})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "carol"


# ---------------------------------------------------------------- jwt_prefer_header mode


def test_jwt_prefer_header_uses_jwt_when_present(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=oidc_token_manager)
    token = make_token(preferred_username="alice")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_prefer_header_jwt_wins_over_header(oidc_token_manager, make_token) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=oidc_token_manager)
    token = make_token(preferred_username="alice")
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token, "X-User-Id": "mallory"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_prefer_header_falls_back_to_header_when_no_jwt(oidc_token_manager) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=oidc_token_manager)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-User-Id": "bob"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "bob"


def test_jwt_prefer_header_no_jwt_no_header_returns_422(oidc_token_manager) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=oidc_token_manager)
    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_jwt_prefer_header_invalid_jwt_returns_401_not_fallback(
    oidc_token_manager, make_token
) -> None:
    app = _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=oidc_token_manager)
    expired_token = make_token(preferred_username="alice", exp=int(time.time()) - 1)
    with TestClient(app) as client:
        resp = client.get(
            "/protected", headers={"X-Auth-Token": expired_token, "X-User-Id": "mallory"}
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_TOKEN_EXPIRED"


def test_jwt_prefer_header_without_token_manager_raises() -> None:
    with pytest.raises(RuntimeError, match="token_manager"):
        _build_app(auth_mode=AuthMode.jwt_prefer_header, token_manager=None)
