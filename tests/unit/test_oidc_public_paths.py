"""T8.1a Red — public-path bypass set (`_PUBLIC_PATHS`, §3.5 rewritten 2026-05-20).

Pins:
  * The 8-path canonical set (adds `/docs/oauth2-redirect` over the prior
    7-path set so Swagger UI's OAuth2 redirect doesn't 401).
  * `_SKIP_PATHS ⊂ _PUBLIC_PATHS` — the structured-logging skip set must NEVER
    drop a path that auth would short-circuit (otherwise an unauthenticated
    bypass would still log per-request — broken contract).
  * Every public path returns 2xx when called without a token, even when auth
    is on (`auth_disabled=False`, `trust_header=False`).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _PUBLIC_PATHS, _x_user_id_middleware
from ragent.middleware.logging import _SKIP_PATHS

_EXPECTED_PUBLIC_PATHS = frozenset(
    {
        "/livez",
        "/readyz",
        "/startupz",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)


def test_public_paths_constant_matches_spec() -> None:
    """§3.5 (2026-05-20) lists exactly these 8 entries."""
    assert _PUBLIC_PATHS == _EXPECTED_PUBLIC_PATHS


def test_skip_paths_is_subset_of_public_paths() -> None:
    """T8.3a invariant — never log a path that bypassed auth."""
    extra = _SKIP_PATHS - _PUBLIC_PATHS
    assert extra == frozenset(), f"_SKIP_PATHS leaks paths not in _PUBLIC_PATHS: {extra}"


@pytest.fixture
def auth_on_client(oidc_token_manager) -> TestClient:
    """TestClient for every parametrized public-path case. Function-scoped to
    inherit ``oidc_token_manager`` (also function-scoped)."""
    from ragent.bootstrap.auth_mode import AuthMode

    app = FastAPI()
    _x_user_id_middleware(app, auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)

    @app.get("/livez")
    def _livez() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def _readyz() -> dict:
        return {"ok": True}

    @app.get("/startupz")
    def _startupz() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    def _metrics() -> str:
        return "metrics"

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", sorted(_EXPECTED_PUBLIC_PATHS))
def test_public_path_bypasses_middleware_without_token(
    auth_on_client: TestClient, path: str
) -> None:
    resp = auth_on_client.get(path)
    # 401/422 = auth failure; 200/307 = bypass (307 is FastAPI's /docs/oauth2-redirect).
    assert resp.status_code in (200, 307), (
        f"{path} returned {resp.status_code}; expected 200/307 (bypass)"
    )


def test_protected_path_still_requires_auth_when_enabled(oidc_token_manager) -> None:
    """Sanity: with auth on, a non-public path without a token MUST fail."""
    from ragent.bootstrap.auth_mode import AuthMode

    app = FastAPI()
    _x_user_id_middleware(app, auth_mode=AuthMode.jwt_header, token_manager=oidc_token_manager)

    @app.get("/protected")
    def _protected(x_user_id: str | None = Header(default=None, alias="X-User-Id")) -> dict:
        return {"user_id": x_user_id}

    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 401, (
            f"/protected returned {resp.status_code}; expected 401 (no token)"
        )
