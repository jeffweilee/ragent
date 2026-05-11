"""Multipart file upload ingest endpoint (spec §4.1 — admin convenience path).

POST /ingest/v1/upload accepts a multipart form with the file bytes and
metadata fields. The server stages bytes to the default MinIO site and
enqueues the pipeline task — identical downstream behaviour to ingest_type
"inline" (server owns the object; delete cleans it up).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.ingest import SOURCE_META_MAX, SOURCE_URL_MAX, IngestMime
from ragent.services.ingest_service import FileTooLarge


def create_router(svc: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/ingest/v1/upload", status_code=202)
    async def upload_document(
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
        file: UploadFile = File(...),
        source_id: str = Form(..., min_length=1),
        source_app: str = Form(..., min_length=1),
        source_title: str = Form(..., min_length=1),
        mime_type: IngestMime = Form(...),
        source_meta: Annotated[str | None, Form(max_length=SOURCE_META_MAX)] = None,
        source_url: Annotated[str | None, Form(max_length=SOURCE_URL_MAX)] = None,
    ):
        data = await file.read()
        try:
            document_id = await svc.create_from_upload(
                create_user=x_user_id,
                source_id=source_id,
                source_app=source_app,
                source_title=source_title,
                mime_type=mime_type,
                data=data,
                source_meta=source_meta,
                source_url=source_url,
            )
        except FileTooLarge:
            return problem(413, HttpErrorCode.INGEST_FILE_TOO_LARGE, "Upload too large")
        return JSONResponse({"document_id": document_id}, status_code=202)

    return router
