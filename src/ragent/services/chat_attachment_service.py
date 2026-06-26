"""T-CAT.11 — ChatAttachmentService: orchestrate upload → store → pipeline → encrypt → persist."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import structlog

from ragent.schemas.attachments import AttachmentMime
from ragent.utility.datetime import to_iso, utcnow

if TYPE_CHECKING:
    from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
    from ragent.repositories.attachment_repository import AttachmentRepository
    from ragent.security.ast_cipher import ASTCipher
    from ragent.storage.document_store import DocumentStore

logger = structlog.get_logger(__name__)


class ChatAttachmentService:
    """Orchestrate attachment upload: store raw bytes → pipeline → encrypt ASTs → persist.

    Workflow:
    1. Store raw file bytes to DocumentStore
    2. Run ChatAttachmentPipeline (load → unprotect → AST build)
    3. Encrypt complete and simplified AST variants
    4. Store encrypted artifacts to DocumentStore
    5. Write attachment metadata and artifacts to database

    Depends only on Protocols (DocumentStore, ASTCipher via interface) + repository (R3).
    """

    def __init__(
        self,
        document_store: DocumentStore,
        ast_cipher: ASTCipher,
        attachment_repository: AttachmentRepository,
        pipeline: ChatAttachmentPipeline,
    ):
        self._doc_store = document_store
        self._ast_cipher = ast_cipher
        self._repo = attachment_repository
        self._pipeline = pipeline

    async def upload(
        self,
        file_bytes: bytes,
        filename: str,
        thread_id: str,
        create_user: str,
        mime_type: AttachmentMime,
    ) -> str:
        """Upload and process an attachment.

        Args:
            file_bytes: Raw file content as bytes
            filename: Original filename
            thread_id: Thread/conversation ID for scoping
            create_user: User who uploaded the file
            mime_type: AttachmentMime type

        Returns:
            attachment_id of the persisted attachment
        """
        attachment_id = str(uuid.uuid4())
        logger.info(
            "chat_attachment.upload_started",
            attachment_id=attachment_id,
            thread_id=thread_id,
            filename=filename,
            mime_type=mime_type.value,
            size_bytes=len(file_bytes),
        )

        stage = "store_raw"
        try:
            self._doc_store.put(
                object_key=f"attachments/{thread_id}/{attachment_id}/raw",
                data=file_bytes,
                content_type=mime_type.value,
            )

            stage = "pipeline_run"
            result = await self._pipeline.run(
                file_bytes=file_bytes, mime_type=mime_type, user_id=create_user, filename=filename
            )
            complete_docs = result["complete"]
            simplified_docs = result["simplified"]

            complete_ast_str = self._ast_to_markdown(complete_docs)
            simplified_ast_str = self._ast_to_markdown(simplified_docs)

            stage = "encrypt_ast"
            created_at = to_iso(utcnow())
            ast_variants = {
                "complete": self._ast_cipher.encrypt_ast(
                    complete_ast_str,
                    attachment_id=attachment_id,
                    ast_type="complete",
                    created_at=created_at,
                ),
                "simplified": self._ast_cipher.encrypt_ast(
                    simplified_ast_str,
                    attachment_id=attachment_id,
                    ast_type="simplified",
                    created_at=created_at,
                ),
            }

            stage = "store_artifacts"
            for ast_type, encrypted in ast_variants.items():
                key = f"attachments/{thread_id}/{attachment_id}/ast-{ast_type}"
                self._doc_store.put(
                    object_key=key,
                    data=json.dumps(encrypted).encode("utf-8"),
                    content_type="application/json",
                )

            stage = "repo_create"
            await self._repo.create(
                attachment_id=attachment_id,
                thread_id=thread_id,
                create_user=create_user,
                filename=filename,
                mime_type=mime_type.value,
                size_bytes=len(file_bytes),
            )

            stage = "repo_add_artifact"
            for ast_type in ast_variants:
                key = f"attachments/{thread_id}/{attachment_id}/ast-{ast_type}"
                await self._repo.add_artifact(
                    attachment_id=attachment_id,
                    ast_type=ast_type,
                    storage_key=key,
                )

            # Promote to READY only after artifacts are durably persisted.
            stage = "repo_update_status"
            await self._repo.update_status(attachment_id, "READY")
        except Exception as exc:
            logger.error(
                "chat_attachment.upload_failed",
                attachment_id=attachment_id,
                thread_id=thread_id,
                stage=stage,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        logger.info(
            "chat_attachment.upload_completed",
            attachment_id=attachment_id,
            thread_id=thread_id,
        )
        return attachment_id

    @staticmethod
    def _ast_to_markdown(docs: list) -> str:
        """Convert list of AST Documents to markdown string."""
        lines = []
        for i, doc in enumerate(docs, 1):
            lines.append(f"[{i}] {doc.content}")
        return "\n".join(lines)
