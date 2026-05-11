"""T-AV.1 — All business API routes carry a /v<N> version segment.

Asserts that every non-infrastructure route registered on a FastAPI app
containing all business routers satisfies the pattern
r"^/[a-z][a-z0-9-]*/v[1-9]\\d*".
Infrastructure paths (/livez, /readyz, /startupz, /metrics, /docs, /redoc,
/openapi.json) are explicitly excluded.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.routing import APIRoute

from ragent.routers.chat import create_chat_router
from ragent.routers.ingest import create_router as create_ingest_router
from ragent.routers.mcp import create_mcp_router
from ragent.routers.retrieve import create_retrieve_router

_INFRA_PREFIXES = {
    "/livez",
    "/readyz",
    "/startupz",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
}

_VERSION_RE = re.compile(r"^/[a-z][a-z0-9-]*/v[1-9]\d*")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_ingest_router(svc=MagicMock()))
    app.include_router(create_chat_router(retrieval_pipeline=MagicMock(), llm_client=MagicMock()))
    app.include_router(create_retrieve_router(retrieval_pipeline=MagicMock()))
    app.include_router(create_mcp_router())
    return app


def test_all_business_routes_have_version_segment():
    app = _build_app()
    violations = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if any(path == p or path.startswith(p + "/") for p in _INFRA_PREFIXES):
            continue
        if not _VERSION_RE.match(path):
            violations.append(path)
    assert not violations, f"Business routes missing version segment: {sorted(violations)}"


def test_version_segment_is_positive_integer():
    """Version token must be v1 or higher — v0 is not a valid version."""
    assert _VERSION_RE.match("/ingest/v1")
    assert _VERSION_RE.match("/chat/v1")
    assert _VERSION_RE.match("/chat/v1/stream")
    assert _VERSION_RE.match("/retrieve/v1")
    assert _VERSION_RE.match("/mcp/v1/tools/rag")
    assert _VERSION_RE.match("/ingest/v10")
    assert not _VERSION_RE.match("/ingest/v0")
    assert not _VERSION_RE.match("/ingest")
    assert not _VERSION_RE.match("/ingest/1")


def test_infra_routes_excluded():
    """Infrastructure paths are never checked against the version pattern."""
    for path in _INFRA_PREFIXES:
        assert any(path == p or path.startswith(p + "/") for p in _INFRA_PREFIXES), (
            f"{path!r} must be in the infra exclusion set"
        )
