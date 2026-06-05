"""T8.D1 Red+Green — Swagger doc auto-derives from the same auth config the middleware uses.

Pins:
  * user_header / none / jwt_prefer_header modes: `components.securitySchemes.UserIdHeader`
    is an `apiKey` in `header` targeting the configured `user_id_header`.
  * jwt_header mode: `components.securitySchemes.JWT` is an `apiKey` in `header`
    targeting the configured `jwt_header`.
  * Every non-public operation tags `security: [{<active_scheme>: []}]`.
  * Public paths (`_PUBLIC_PATHS`) carry NO `security` key.
  * A non-default `jwt_header` propagates verbatim into the scheme `name`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ragent.bootstrap.app import _PUBLIC_PATHS
from ragent.bootstrap.auth_mode import AuthMode
from ragent.bootstrap.openapi import install_openapi


def _install(
    *,
    auth_mode: AuthMode,
    user_id_header: str = "X-User-Id",
    jwt_header: str = "X-Auth-Token",
) -> dict[str, Any]:
    """Build a tiny app with one public + one protected route, install_openapi, return schema."""
    app = FastAPI()

    @app.get("/livez")
    async def _public() -> dict:
        return {"ok": True}

    @app.post("/chat/v1")
    async def _protected() -> dict:
        return {}

    install_openapi(
        app,
        auth_mode=auth_mode,
        user_id_header=user_id_header,
        jwt_header=jwt_header,
        public_paths=_PUBLIC_PATHS,
    )
    return app.openapi()


def test_user_header_mode_publishes_user_id_apikey_scheme() -> None:
    schemes = _install(auth_mode=AuthMode.user_header)["components"]["securitySchemes"]
    assert "UserIdHeader" in schemes
    assert schemes["UserIdHeader"]["type"] == "apiKey"
    assert schemes["UserIdHeader"]["in"] == "header"
    assert schemes["UserIdHeader"]["name"] == "X-User-Id"
    assert "JWT" not in schemes


def test_none_mode_publishes_user_id_apikey_scheme() -> None:
    schemes = _install(auth_mode=AuthMode.none)["components"]["securitySchemes"]
    assert "UserIdHeader" in schemes
    assert "JWT" not in schemes


def test_jwt_prefer_header_mode_publishes_user_id_apikey_scheme() -> None:
    schemes = _install(auth_mode=AuthMode.jwt_prefer_header)["components"]["securitySchemes"]
    assert "UserIdHeader" in schemes
    assert "JWT" not in schemes


def test_jwt_header_mode_publishes_jwt_apikey_scheme() -> None:
    schemes = _install(auth_mode=AuthMode.jwt_header)["components"]["securitySchemes"]
    assert "JWT" in schemes
    assert schemes["JWT"]["type"] == "apiKey"
    assert schemes["JWT"]["in"] == "header"
    assert schemes["JWT"]["name"] == "X-Auth-Token"
    assert "UserIdHeader" not in schemes


def test_protected_operation_references_active_scheme_in_jwt_mode() -> None:
    op = _install(auth_mode=AuthMode.jwt_header)["paths"]["/chat/v1"]["post"]
    assert op.get("security") == [{"JWT": []}]


def test_protected_operation_references_active_scheme_in_user_header_mode() -> None:
    op = _install(auth_mode=AuthMode.user_header)["paths"]["/chat/v1"]["post"]
    assert op.get("security") == [{"UserIdHeader": []}]


def test_public_operation_has_no_security() -> None:
    op = _install(auth_mode=AuthMode.jwt_header)["paths"]["/livez"]["get"]
    assert "security" not in op


def test_custom_jwt_header_propagates_to_scheme_name() -> None:
    schemes = _install(auth_mode=AuthMode.jwt_header, jwt_header="X-Custom-Auth")["components"][
        "securitySchemes"
    ]
    assert schemes["JWT"]["name"] == "X-Custom-Auth"


def test_custom_user_id_header_propagates_to_scheme_name() -> None:
    schemes = _install(auth_mode=AuthMode.user_header, user_id_header="X-Tenant-User")[
        "components"
    ]["securitySchemes"]
    assert schemes["UserIdHeader"]["name"] == "X-Tenant-User"


def test_security_list_is_not_shared_across_operations() -> None:
    """Mutating one operation's `security` MUST NOT bleed into another's."""
    app = FastAPI()

    @app.post("/chat/v1")
    async def _a() -> dict:
        return {}

    @app.post("/ingest/v1")
    async def _b() -> dict:
        return {}

    install_openapi(
        app,
        auth_mode=AuthMode.jwt_header,
        user_id_header="X-User-Id",
        jwt_header="X-Auth-Token",
        public_paths=_PUBLIC_PATHS,
    )
    schema = app.openapi()
    op_a = schema["paths"]["/chat/v1"]["post"]
    op_b = schema["paths"]["/ingest/v1"]["post"]
    assert op_a["security"] is not op_b["security"]


def test_fastapi_metadata_fields_are_preserved() -> None:
    """install_openapi must NOT silently drop FastAPI metadata fields."""
    app = FastAPI(
        title="ragent",
        version="2.0",
        description="rag service",
        openapi_tags=[{"name": "chat", "description": "Chat endpoints"}],
        servers=[{"url": "https://api.example.com", "description": "prod"}],
        terms_of_service="https://example.com/tos",
        contact={"name": "ops", "email": "ops@example.com"},
        license_info={"name": "Apache 2.0"},
    )

    @app.post("/chat/v1", tags=["chat"])
    async def _chat() -> dict:
        return {}

    install_openapi(
        app,
        auth_mode=AuthMode.user_header,
        user_id_header="X-User-Id",
        jwt_header="X-Auth-Token",
        public_paths=_PUBLIC_PATHS,
    )
    schema = app.openapi()
    assert schema["info"]["title"] == "ragent"
    assert schema["info"]["version"] == "2.0"
    assert schema["info"]["description"] == "rag service"
    assert schema["info"]["termsOfService"] == "https://example.com/tos"
    assert schema["info"]["contact"] == {"name": "ops", "email": "ops@example.com"}
    assert schema["info"]["license"] == {"name": "Apache 2.0"}
    assert schema["tags"] == [{"name": "chat", "description": "Chat endpoints"}]
    assert schema["servers"] == [{"url": "https://api.example.com", "description": "prod"}]
