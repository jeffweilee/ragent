"""T-CAT.12 — Attachments upload and retrieval endpoints (nested under /chatagent/v3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ragent.auth.deps import get_user_id
from ragent.schemas.attachments import AttachmentMime

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


class ListAttachmentsResponse(BaseModel):
    """Response from GET /chatagent/v3/attachments."""

    attachments: list[AttachmentInfo]


def create_attachments_router(
    service: ChatAttachmentService,
    repository: AttachmentRepository,
) -> APIRouter:
    """Create attachments router with injected dependencies.

    Args:
        service: ChatAttachmentService instance
        repository: AttachmentRepository instance

    Returns:
        APIRouter with POST/GET attachment endpoints
    """
    router = APIRouter(prefix="/chatagent/v3/attachments", tags=["attachments"])

    @router.post("/upload", response_model=UploadAttachmentResponse, status_code=200)
    async def upload_attachment(
        file: Annotated[UploadFile, File()],
        threadId: Annotated[str, Form()],
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> UploadAttachmentResponse:
        """Upload a file to a conversation thread."""
        user_id = user_id or "anonymous"

        file_bytes = await file.read()

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

        attachment_id = await service.upload(
            file_bytes=file_bytes,
            filename=file.filename or "unknown",
            thread_id=threadId,
            create_user=user_id,
            mime_type=mime_type,
        )

        return UploadAttachmentResponse(attachmentId=attachment_id)

    @router.get("", response_model=ListAttachmentsResponse, status_code=200)
    async def list_attachments(
        threadId: str,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> ListAttachmentsResponse:
        """List attachments for a conversation thread."""
        logger.info("attachments.list_request", thread_id=threadId, user_id=user_id)
        attachments = await repository.list_by_thread(threadId)

        return ListAttachmentsResponse(
            attachments=[
                AttachmentInfo(
                    attachmentId=att.attachment_id,
                    filename=att.filename,
                    mimeType=att.mime_type,
                    sizeBytes=att.size_bytes,
                    status=att.status,
                )
                for att in attachments
            ]
        )

    return router
