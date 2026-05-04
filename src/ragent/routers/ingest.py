"""T2.14 — Ingest router: POST/GET/DELETE/LIST endpoints (spec §4.1, B5, B11, S23)."""

from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from ragent.errors.problem import problem
from ragent.services.ingest_service import FileTooLarge, MimeNotAllowed


def create_router(svc: Any) -> APIRouter:  # noqa: B008
    router = APIRouter()

    @router.post("/ingest", status_code=202)
    async def create_document(  # noqa: B008
        file: Annotated[UploadFile, File()],
        source_id: Annotated[str | None, Form()] = None,
        source_app: Annotated[str | None, Form()] = None,
        source_title: Annotated[str | None, Form()] = None,
        source_workspace: Annotated[str | None, Form()] = None,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        if not x_user_id:
            return problem(422, "INGEST_VALIDATION", "Missing X-User-Id header", errors=[])

        field_errors = []
        if not source_id:
            field_errors.append({"field": "source_id", "message": "required"})
        if not source_app:
            field_errors.append({"field": "source_app", "message": "required"})
        if not source_title:
            field_errors.append({"field": "source_title", "message": "required"})
        if field_errors:
            return problem(
                422,
                "INGEST_VALIDATION",
                "Validation error",
                detail="Required fields missing",
                errors=field_errors,
            )

        data = await file.read()
        try:
            doc_id = svc.create(
                create_user=x_user_id,
                source_id=source_id,
                source_app=source_app,
                source_title=source_title,
                source_workspace=source_workspace,
                file_data=io.BytesIO(data),
                file_size=len(data),
                content_type=file.content_type or "application/octet-stream",
            )
        except MimeNotAllowed:
            return problem(415, "INGEST_MIME_UNSUPPORTED", "Unsupported media type")
        except FileTooLarge:
            return problem(413, "INGEST_FILE_TOO_LARGE", "File too large")

        return JSONResponse({"task_id": doc_id}, status_code=202)

    @router.get("/ingest/{document_id}")
    async def get_document(
        document_id: str,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        doc = svc.get(document_id)
        if doc is None:
            return problem(404, "INGEST_NOT_FOUND", "Document not found")
        return {
            "document_id": doc.document_id,
            "status": doc.status,
            "attempt": doc.attempt,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        }

    @router.delete("/ingest/{document_id}", status_code=204)
    async def delete_document(
        document_id: str,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        svc.delete(document_id)
        return Response(status_code=204)

    @router.get("/ingest")
    async def list_documents(
        after: str | None = Query(None),
        limit: int = Query(100),
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        result = svc.list(after=after, limit=limit)
        items = [
            {
                "document_id": doc.document_id,
                "status": doc.status,
                "source_id": doc.source_id,
                "source_app": doc.source_app,
                "source_title": doc.source_title,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            }
            for doc in result.items
        ]
        return {"items": items, "next_cursor": result.next_cursor}

    return router
