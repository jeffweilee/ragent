"""T-CAT.11 / T-CAT.W2 — ChatAttachmentService: upload (fast intake) + process (async worker)."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import structlog

from ragent.errors.codes import TaskErrorCode
from ragent.schemas.attachments import AttachmentMime
from ragent.utility.datetime import to_iso, utcnow

if TYPE_CHECKING:
    from ragent.bootstrap.dispatcher import TaskiqDispatcher
    from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
    from ragent.repositories.attachment_repository import AttachmentRepository
    from ragent.security.ast_cipher import ASTCipher
    from ragent.storage.document_store import DocumentStore

logger = structlog.get_logger(__name__)


class ChatAttachmentService:
    """Orchestrate attachment upload (fast intake) and processing (async worker).

    `upload()` stores raw bytes, writes the UPLOADED row, and dispatches the
    `attachment.process` task — it returns as soon as those three steps
    complete, without running the pipeline/encrypt/persist sequence inline.

    `process()` is invoked by the `attachment.process` worker task: claims
    the row (UPLOADED→PROCESSING), runs ChatAttachmentPipeline, encrypts both
    AST variants, persists the encrypted artifacts, and promotes to READY —
    or terminalizes to FAILED with error_code/error_reason on any exception.

    Depends only on Protocols (DocumentStore, ASTCipher, TaskiqDispatcher via
    interface) + repository (R3).
    """

    def __init__(
        self,
        document_store: DocumentStore,
        ast_cipher: ASTCipher,
        attachment_repository: AttachmentRepository,
        pipeline: ChatAttachmentPipeline,
        dispatcher: TaskiqDispatcher,
    ):
        self._doc_store = document_store
        self._ast_cipher = ast_cipher
        self._repo = attachment_repository
        self._pipeline = pipeline
        self._dispatcher = dispatcher

    async def upload(
        self,
        file_bytes: bytes,
        filename: str,
        thread_id: str,
        create_user: str,
        mime_type: AttachmentMime,
    ) -> str:
        """Store raw bytes, write the UPLOADED row, and dispatch processing.

        Args:
            file_bytes: Raw file content as bytes
            filename: Original filename
            thread_id: Thread/conversation ID for scoping
            create_user: User who uploaded the file
            mime_type: AttachmentMime type

        Returns:
            attachment_id of the UPLOADED row (processing happens async)
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

            stage = "repo_create"
            await self._repo.create(
                attachment_id=attachment_id,
                thread_id=thread_id,
                create_user=create_user,
                filename=filename,
                mime_type=mime_type.value,
                size_bytes=len(file_bytes),
            )

            stage = "enqueue_process"
            await self._dispatcher.enqueue("attachment.process", attachment_id=attachment_id)
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

    async def process(self, attachment_id: str) -> None:
        """Worker-side processing: claim → pipeline → encrypt → persist → READY/FAILED.

        No-ops (graceful skip) if the row is already claimed, terminal, or
        missing. Never re-raises — TaskIQ is at-most-once with no reconciler
        in this scope, so an escaped exception would just strand the row.
        """
        claimed = await self._repo.claim_for_processing(attachment_id)
        if claimed is None:
            logger.info("chat_attachment.process_skipped", attachment_id=attachment_id)
            return

        thread_id = claimed.thread_id
        stage = "fetch_raw"
        try:
            file_bytes = self._doc_store.get(f"attachments/{thread_id}/{attachment_id}/raw")

            stage = "pipeline_run"
            result = await self._pipeline.run(
                file_bytes=file_bytes,
                mime_type=AttachmentMime(claimed.mime_type),
                user_id=claimed.create_user,
                filename=claimed.filename,
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
            error_code = getattr(exc, "error_code", None) or TaskErrorCode.PIPELINE_UNEXPECTED_ERROR
            reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                "chat_attachment.process_failed",
                attachment_id=attachment_id,
                thread_id=thread_id,
                stage=stage,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await self._repo.update_status(
                attachment_id, "FAILED", error_code=error_code, error_reason=reason
            )
            return

        logger.info(
            "chat_attachment.process_completed",
            attachment_id=attachment_id,
            thread_id=thread_id,
        )

    @staticmethod
    def _ast_to_markdown(docs: list) -> str:
        """Convert list of AST Documents to markdown string."""
        lines = []
        for i, doc in enumerate(docs, 1):
            lines.append(f"[{i}] {doc.content}")
        return "\n".join(lines)
