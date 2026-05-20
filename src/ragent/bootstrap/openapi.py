"""T8.D1 — Swagger doc generator that mirrors the auth middleware's config.

``install_openapi`` swaps ``app.openapi`` for a callable that:

  * registers one ``apiKey`` security scheme on ``components.securitySchemes``
    matching the active auth mode (``UserIdHeader`` for trust-header mode,
    ``JWT`` for JWT mode), with ``name`` set to the SAME header literal the
    middleware reads from the request;
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

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})


def is_trust_header_mode(*, auth_disabled: bool, trust_header: bool) -> bool:
    """Single source of truth for the middleware/doc mode switch (§3.5).

    Mirrors ``_x_user_id_middleware``'s ``trust_header_mode`` so the gate and
    the Swagger Authorize button cannot disagree on which header is active.
    """
    return auth_disabled or trust_header


def install_openapi(
    app: FastAPI,
    *,
    auth_disabled: bool,
    trust_header: bool,
    user_id_header: str,
    jwt_header: str,
    public_paths: frozenset[str],
) -> None:
    if is_trust_header_mode(auth_disabled=auth_disabled, trust_header=trust_header):
        scheme_name = "UserIdHeader"
        scheme: dict[str, Any] = {
            "type": "apiKey",
            "in": "header",
            "name": user_id_header,
            "description": (
                "Trust-header mode: client asserts identity via this header. "
                "Used when RAGENT_AUTH_DISABLED=true or "
                "RAGENT_TRUST_X_USER_ID_HEADER=true."
            ),
        }
    else:
        scheme_name = "JWT"
        scheme = {
            "type": "apiKey",
            "in": "header",
            "name": jwt_header,
            "description": (
                "OIDC JWT verified against JWKS. Send the raw token in this "
                "header (no `Bearer ` prefix). Required when "
                "RAGENT_AUTH_DISABLED=false and RAGENT_TRUST_X_USER_ID_HEADER=false."
            ),
        }

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
        for path, ops in schema.get("paths", {}).items():
            if path in public_paths:
                continue
            for method, op in ops.items():
                if method in _HTTP_METHODS and isinstance(op, dict):
                    # Fresh list per operation — mutation by downstream consumers
                    # (Swagger UI, codegen) doesn't bleed across ops.
                    op["security"] = [{scheme_name: []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]
