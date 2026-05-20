"""User-id middleware bypass + JWT decode path (C9, T8.2 / §3.5)."""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _NO_USER_ID_PATHS, _x_user_id_middleware


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "none"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


def _build_app(
    *,
    user_id_header: str = "X-User-Id",
    jwt_header: str = "X-Auth-Token",
    jwt_claim: str = "preferred_username",
    trust_header: bool = True,
    auth_disabled: bool = True,
) -> FastAPI:
    app = FastAPI()
    _x_user_id_middleware(
        app,
        user_id_header=user_id_header,
        jwt_header=jwt_header,
        jwt_claim=jwt_claim,
        trust_header=trust_header,
        auth_disabled=auth_disabled,
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


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/metrics"])
def test_probe_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_swagger_doc_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


def test_protected_path_requires_user_id() -> None:
    with TestClient(_build_app()) as client:
        resp = client.get("/protected")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_no_user_id_paths_includes_docs_and_probes() -> None:
    expected = {"/livez", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"}
    assert expected <= _NO_USER_ID_PATHS


# --- T8.2 JWT path (auth_disabled=false, trust_header=false) ---


def test_jwt_mode_extracts_claim_and_injects_user_id_header() -> None:
    app = _build_app(trust_header=False, auth_disabled=False)
    token = _jwt({"exp": int(time.time()) + 60, "preferred_username": "alice"})
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_mode_ignores_user_id_header_when_token_present() -> None:
    app = _build_app(trust_header=False, auth_disabled=False)
    token = _jwt({"exp": int(time.time()) + 60, "preferred_username": "alice"})
    with TestClient(app) as client:
        resp = client.get(
            "/protected", headers={"X-Auth-Token": token, "X-User-Id": "mallory"}
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_jwt_mode_missing_token_returns_401() -> None:
    app = _build_app(trust_header=False, auth_disabled=False)
    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_TOKEN_INVALID"


def test_jwt_mode_expired_returns_401_expired() -> None:
    app = _build_app(trust_header=False, auth_disabled=False)
    token = _jwt({"exp": int(time.time()) - 1, "preferred_username": "alice"})
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_TOKEN_EXPIRED"


def test_jwt_mode_missing_claim_returns_401_claim_missing() -> None:
    app = _build_app(trust_header=False, auth_disabled=False)
    token = _jwt({"exp": int(time.time()) + 60})  # no preferred_username
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_CLAIM_MISSING"


def test_trust_header_true_uses_header_directly() -> None:
    app = _build_app(trust_header=True, auth_disabled=False)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-User-Id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_auth_disabled_uses_header_regardless_of_trust() -> None:
    app = _build_app(trust_header=False, auth_disabled=True)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-User-Id": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_custom_user_id_header_name() -> None:
    app = _build_app(user_id_header="X-Whoami", trust_header=True, auth_disabled=False)
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Whoami": "alice"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"


def test_custom_jwt_header_and_claim_round_trip() -> None:
    app = _build_app(
        jwt_header="X-Token",
        jwt_claim="sub",
        trust_header=False,
        auth_disabled=False,
    )
    token = _jwt({"exp": int(time.time()) + 60, "sub": "carol"})
    with TestClient(app) as client:
        resp = client.get("/protected", headers={"X-Token": token})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "carol"
