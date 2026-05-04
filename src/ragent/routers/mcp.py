"""T6.1 — MCP stub: POST /mcp/tools/rag returns 501 (Phase 1, §4.1.2, S8)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from ragent.errors.problem import problem


def create_mcp_router() -> APIRouter:
    router = APIRouter(prefix="/mcp")

    @router.post("/tools/rag")
    async def rag_tool() -> Response:
        return problem(501, "MCP_NOT_IMPLEMENTED", "MCP RAG tool not implemented in Phase 1")

    return router
