"""T2v.25 — Ingest router v2: JSON-only POST /ingest (spec §4.1).

Discriminated body validates via Pydantic before reaching the service.
Multipart is not supported — old callers fall through to a clean 415/422
because `content-type: multipart/...` cannot satisfy the JSON discriminator.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.ingest import FileIngestRequest, IngestRequest, InlineIngestRequest
from ragent.services.ingest_service import (
    FileTooLarge,
    MimeNotAllowed,
    ObjectNotFoundError,
    UnknownMinioSiteError,
)

_INGEST_BODY_SCHEMA = {
    "oneOf": [
        InlineIngestRequest.model_json_schema(),
        FileIngestRequest.model_json_schema(),
    ],
    "discriminator": {"propertyName": "ingest_type"},
}

logger = structlog.get_logger(__name__)


def _is_mime_error(errors: list[dict]) -> bool:
    return any(any(part == "mime_type" for part in e.get("loc", ())) for e in errors)


def _validation_problem(exc: ValidationError):
    raw = exc.errors()
    flat = [
        {"field": ".".join(str(p) for p in e.get("loc", ())), "message": e.get("msg", "")}
        for e in raw
    ]
    if _is_mime_error(raw):
        logger.warning(
            "ingest.validation_failed",
            error_code=HttpErrorCode.INGEST_MIME_UNSUPPORTED,
            http_status=415,
            field_count=len(flat),
        )
        return problem(
            415,
            HttpErrorCode.INGEST_MIME_UNSUPPORTED,
            "Unsupported media type",
            errors=flat,
        )
    logger.warning(
        "ingest.validation_failed",
        error_code=HttpErrorCode.INGEST_VALIDATION,
        http_status=422,
        field_count=len(flat),
    )
    return problem(
        422,
        HttpErrorCode.INGEST_VALIDATION,
        "Validation error",
        detail="Request body failed validation",
        errors=flat,
    )


def create_router(svc: Any) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/ingest",
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _INGEST_BODY_SCHEMA}},
            }
        },
    )
    async def create_document(
        request: Request,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        try:
            body = await request.json()
        except Exception:
            return problem(415, HttpErrorCode.INGEST_MIME_UNSUPPORTED, "JSON body required")
        from pydantic import TypeAdapter

        try:
            payload = TypeAdapter(IngestRequest).validate_python(body)
        except ValidationError as exc:
            return _validation_problem(exc)

        try:
            doc_id = await svc.create(create_user=x_user_id, request=payload)
        except MimeNotAllowed:
            return problem(415, HttpErrorCode.INGEST_MIME_UNSUPPORTED, "Unsupported media type")
        except FileTooLarge:
            return problem(413, HttpErrorCode.INGEST_FILE_TOO_LARGE, "Inline content too large")
        except UnknownMinioSiteError:
            return problem(422, HttpErrorCode.INGEST_MINIO_SITE_UNKNOWN, "Unknown minio_site")
        except ObjectNotFoundError:
            return problem(
                422,
                HttpErrorCode.INGEST_OBJECT_NOT_FOUND,
                "Object not found at minio_site/object_key",
            )

        return JSONResponse({"document_id": doc_id}, status_code=202)

    @router.get("/ingest/{document_id}")
    async def get_document(
        document_id: str,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        doc = await svc.get(document_id)
        if doc is None:
            logger.info(
                "ingest.not_found",
                document_id=document_id,
                error_code=HttpErrorCode.INGEST_NOT_FOUND,
                http_status=404,
            )
            return problem(404, HttpErrorCode.INGEST_NOT_FOUND, "Document not found")
        return {
            "document_id": doc.document_id,
            "status": doc.status,
            "attempt": doc.attempt,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            "ingest_type": doc.ingest_type,
            "minio_site": doc.minio_site,
            "source_id": doc.source_id,
            "source_app": doc.source_app,
            "source_title": doc.source_title,
            "source_url": doc.source_url,
            "error_code": getattr(doc, "error_code", None),
            "error_reason": getattr(doc, "error_reason", None),
        }

    @router.delete("/ingest/{document_id}", status_code=204)
    async def delete_document(
        document_id: str,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        await svc.delete(document_id)
        return Response(status_code=204)

    @router.get("/ingest")
    async def list_documents(
        after: str | None = Query(None),
        limit: int = Query(100),
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        result = await svc.list(after=after, limit=limit)
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
