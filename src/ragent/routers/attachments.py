"""T-CAT.12 — Attachments upload and retrieval endpoints (nested under /chatagent/v3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ragent.auth.deps import get_user_id
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.attachments import AttachmentMime
from ragent.services.chat_attachment_service import (
    ATTACHMENT_MAX_SIZE_BYTES_DEFAULT,
    FileTooLarge,
)

if TYPE_CHECKING:
    from ragent.repositories.attachment_repository import AttachmentRepository
    from ragent.services.chat_attachment_service import ChatAttachmentService

logger = structlog.get_logger(__name__)


class UploadAttachmentResponse(BaseModel):
    """Response from POST /chatagent/v3/attachments/upload."""

    attachmentId: str


class AttachmentInfo(BaseModel):
    """Single attachment metadata in list response."""

    attachmentId: str
    filename: str
    mimeType: str
    sizeBytes: int
    status: str
    errorCode: str | None = None
    errorReason: str | None = None


class ListAttachmentsResponse(BaseModel):
    """Response from GET /chatagent/v3/attachments."""

    attachments: list[AttachmentInfo]


def _to_attachment_info(att) -> AttachmentInfo:
    return AttachmentInfo(
        attachmentId=att.attachment_id,
        filename=att.filename,
        mimeType=att.mime_type,
        sizeBytes=att.size_bytes,
        status=att.status,
        errorCode=att.error_code,
        errorReason=att.error_reason,
    )


def create_attachments_router(
    service: ChatAttachmentService,
    repository: AttachmentRepository,
    max_size_bytes: int = ATTACHMENT_MAX_SIZE_BYTES_DEFAULT,
) -> APIRouter:
    """Create attachments router with injected dependencies.

    Args:
        service: ChatAttachmentService instance
        repository: AttachmentRepository instance
        max_size_bytes: Upload size cap; oversized files are rejected with 413

    Returns:
        APIRouter with POST/GET attachment endpoints
    """
    router = APIRouter(prefix="/chatagent/v3/attachments", tags=["attachments"])

    @router.post("/upload", response_model=UploadAttachmentResponse, status_code=202)
    async def upload_attachment(
        file: Annotated[UploadFile, File()],
        threadId: Annotated[str, Form()],
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> UploadAttachmentResponse:
        """Upload a file to a conversation thread."""
        user_id = user_id or "anonymous"

        def reject_too_large(size_bytes: int) -> UploadAttachmentResponse:
            logger.warning(
                "attachments.upload_rejected_size",
                thread_id=threadId,
                filename=file.filename or "unknown",
                user_id=user_id,
                size_bytes=size_bytes,
                max_size_bytes=max_size_bytes,
            )
            return problem(413, HttpErrorCode.ATTACHMENT_TOO_LARGE, "Attachment too large")

        # Early rejection when the client provides Content-Length for the part,
        # avoiding a full read into memory before the service-level check below
        # (which catches transfers, e.g. chunked, that omit Content-Length).
        if file.size is not None and file.size > max_size_bytes:
            response = reject_too_large(file.size)
            await file.close()
            return response

        file_bytes = await file.read()
        await file.close()

        mime_str = file.content_type or "text/plain"
        try:
            mime_type = AttachmentMime(mime_str)
        except ValueError as e:
            logger.warning(
                "attachments.upload_rejected_mime",
                thread_id=threadId,
                mime_type=mime_str,
                user_id=user_id,
            )
            raise HTTPException(status_code=415, detail=f"Unsupported MIME type: {mime_str}") from e

        logger.info(
            "attachments.upload_request",
            thread_id=threadId,
            filename=file.filename or "unknown",
            user_id=user_id,
            size_bytes=len(file_bytes),
        )

        try:
            attachment_id = await service.upload(
                file_bytes=file_bytes,
                filename=file.filename or "unknown",
                thread_id=threadId,
                create_user=user_id,
                mime_type=mime_type,
            )
        except FileTooLarge:
            return reject_too_large(len(file_bytes))

        return UploadAttachmentResponse(attachmentId=attachment_id)

    @router.get("", response_model=ListAttachmentsResponse, status_code=200)
    async def list_attachments(
        threadId: str,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> ListAttachmentsResponse:
        """List attachments for a conversation thread."""
        user_id = user_id or "anonymous"
        logger.info("attachments.list_request", thread_id=threadId, user_id=user_id)
        attachments = await repository.list_by_thread(threadId, create_user=user_id)

        return ListAttachmentsResponse(
            attachments=[_to_attachment_info(att) for att in attachments]
        )

    @router.get("/{attachmentId}", response_model=AttachmentInfo, status_code=200)
    async def get_attachment(
        attachmentId: str,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ):
        """Poll a single attachment's processing status."""
        user_id = user_id or "anonymous"
        att = await repository.get(attachmentId, create_user=user_id)
        if att is None:
            logger.info("attachments.not_found", attachment_id=attachmentId, user_id=user_id)
            return problem(404, HttpErrorCode.ATTACHMENT_NOT_FOUND, "Attachment not found")

        return _to_attachment_info(att)

    return router
