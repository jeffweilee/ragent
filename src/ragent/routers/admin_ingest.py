"""Multipart file upload ingest endpoint (spec §4.1 — admin convenience path).

POST /ingest/v1/upload accepts a multipart form with the file bytes and
metadata fields. The server stages bytes to the default MinIO site and
enqueues the pipeline task — identical downstream behaviour to ingest_type
"inline" (server owns the object; delete cleans it up).
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.ingest import SOURCE_META_MAX, SOURCE_URL_MAX, IngestMime
from ragent.security.file_signature import MagicByteMismatchError, assert_magic_byte
from ragent.services.ingest_service import FileTooLarge

_MAX_UPLOAD_BYTES = int(os.environ.get("INGEST_INLINE_MAX_BYTES", "10485760"))


def create_router(svc: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/ingest/v1/upload", status_code=202)
    async def upload_document(
        file: Annotated[UploadFile, File()],
        source_id: Annotated[str, Form(min_length=1)],
        source_app: Annotated[str, Form(min_length=1)],
        source_title: Annotated[str, Form(min_length=1)],
        mime_type: Annotated[IngestMime, Form()],
        source_meta: Annotated[str | None, Form(max_length=SOURCE_META_MAX)] = None,
        source_url: Annotated[str | None, Form(max_length=SOURCE_URL_MAX)] = None,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ):
        # Early rejection when the client provides Content-Length for the part,
        # avoiding a full read into memory before the service-level size check.
        if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
            await file.close()
            return problem(413, HttpErrorCode.INGEST_FILE_TOO_LARGE, "Upload too large")
        try:
            data = await file.read()
            assert_magic_byte(mime_type, data)
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
        except MagicByteMismatchError as exc:
            return problem(exc.http_status, exc.error_code, str(exc))
        except FileTooLarge:
            return problem(413, HttpErrorCode.INGEST_FILE_TOO_LARGE, "Upload too large")
        finally:
            await file.close()
        return JSONResponse({"document_id": document_id}, status_code=202)

    return router
