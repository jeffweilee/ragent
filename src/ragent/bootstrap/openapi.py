"""T8.D1 — Swagger doc generator that mirrors the auth middleware's config.

``install_openapi`` swaps ``app.openapi`` for a callable that:

  * registers one ``apiKey`` security scheme on ``components.securitySchemes``
    matching the active auth mode (``UserIdHeader`` for header-based modes,
    ``JWT`` for jwt_header mode), with ``name`` set to the SAME header literal
    the middleware reads from the request;
  * tags every non-public operation with ``security: [{<scheme>: []}]`` so
    Swagger UI's *Authorize* dialog applies to the whole protected surface;
  * leaves every path in ``public_paths`` free of any ``security`` field —
    those endpoints are auth-free per ``_PUBLIC_PATHS`` (§3.5).

The same env-resolved values that wire ``_x_user_id_middleware`` are passed
here, so the docs cannot drift from the runtime gate.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from ragent.bootstrap.auth_mode import AuthMode

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
_JWT_SCHEME = "JWT"


def _jwt_scheme(jwt_header: str) -> dict[str, Any]:
    return {
        "type": "apiKey",
        "in": "header",
        "name": jwt_header,
        "description": (
            "OIDC JWT verified against JWKS. Send the raw token in this "
            "header (no `Bearer ` prefix). Required when "
            "RAGENT_AUTH_MODE=jwt_header."
        ),
    }


def _jwt_required_security(scheme_name: str, *, auth_mode: AuthMode) -> list[dict[str, list[str]]]:
    """Security requirement for an operation that needs the raw JWT header.

    ``jwt_prefer_header``: the JWT alone resolves identity *and* is the token we
    forward, so it satisfies the operation on its own. ``user_header`` / ``none``:
    identity comes from the user-id header, so BOTH are needed — expressed as one
    dict, which OpenAPI reads as AND (a list of dicts would read as OR).
    """
    if auth_mode == AuthMode.jwt_prefer_header:
        return [{_JWT_SCHEME: []}]
    return [{scheme_name: [], _JWT_SCHEME: []}]


def is_trust_header_mode(*, auth_mode: AuthMode) -> bool:
    """True when the active mode does NOT require JWT verification (§3.5)."""
    return auth_mode != AuthMode.jwt_header


def install_openapi(
    app: FastAPI,
    *,
    auth_mode: AuthMode,
    user_id_header: str,
    jwt_header: str,
    public_paths: frozenset[str],
    jwt_required_paths: frozenset[str] = frozenset(),
) -> None:
    """Publish the active auth scheme and tag every protected operation.

    ``jwt_required_paths`` names operations that need the raw JWT header even
    when it is not the mode's identity scheme — currently only
    ``POST /pat/v1/authorize``, which forwards the caller's SSO id token to the
    PAT init service. Without this, a trust-header deployment's schema would
    advertise only the user-id header and every Swagger/codegen caller would
    receive ``401 PAT_REAUTH_REQUIRED`` (Codex review PR #240, P2).
    """
    if is_trust_header_mode(auth_mode=auth_mode):
        scheme_name = "UserIdHeader"
        scheme: dict[str, Any] = {
            "type": "apiKey",
            "in": "header",
            "name": user_id_header,
            "description": (
                f"Auth mode {auth_mode!r}: client asserts identity via this header "
                "(none/user_header/jwt_prefer_header modes)."
            ),
        }
    else:
        scheme_name = _JWT_SCHEME
        scheme = _jwt_scheme(jwt_header)

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
            terms_of_service=app.terms_of_service,
            contact=app.contact,
            license_info=app.license_info,
        )
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes[scheme_name] = scheme
        # A path needing the raw JWT under a trust-header mode gets the JWT
        # scheme published alongside the identity one.
        extra_jwt = jwt_required_paths - public_paths if scheme_name != _JWT_SCHEME else frozenset()
        if extra_jwt:
            schemes[_JWT_SCHEME] = _jwt_scheme(jwt_header)
        for path, ops in schema.get("paths", {}).items():
            if path in public_paths:
                continue
            for method, op in ops.items():
                if method in _HTTP_METHODS and isinstance(op, dict):
                    # Fresh list per operation — mutation by downstream consumers
                    # (Swagger UI, codegen) doesn't bleed across ops.
                    op["security"] = (
                        _jwt_required_security(scheme_name, auth_mode=auth_mode)
                        if path in extra_jwt
                        else [{scheme_name: []}]
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]
