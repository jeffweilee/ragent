"""T-CAT.11 / T-CAT.W2 — ChatAttachmentService: upload (fast intake) + process (async worker)."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING

import anyio
import structlog

from ragent.errors.codes import TaskErrorCode
from ragent.schemas.attachments import ARTIFACT_CONTENT_TYPE, AttachmentMime
from ragent.utility.id_gen import new_id

if TYPE_CHECKING:
    from ragent.bootstrap.dispatcher import TaskiqDispatcher
    from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
    from ragent.repositories.attachment_repository import AttachmentRepository, AttachmentRow
    from ragent.security.ast_cipher import ASTCipher
    from ragent.storage.document_store import DocumentStore

logger = structlog.get_logger(__name__)

# Spec default; composition.py reads ATTACHMENT_MAX_SIZE_BYTES env and passes
# the runtime value to both the router (cheap early check) and this service
# (authoritative post-read check) — mirrors ingest_service.py's split.
ATTACHMENT_MAX_SIZE_BYTES_DEFAULT = 50 * 1024 * 1024


class FileTooLarge(Exception):
    pass


def _raw_storage_key(thread_id: str, attachment_id: str) -> str:
    return f"attachments/{thread_id}/{attachment_id}/raw"


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
        max_size_bytes: int = ATTACHMENT_MAX_SIZE_BYTES_DEFAULT,
    ):
        self._doc_store = document_store
        self._ast_cipher = ast_cipher
        self._repo = attachment_repository
        self._pipeline = pipeline
        self._dispatcher = dispatcher
        self._max_size_bytes = max_size_bytes

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

        Raises:
            FileTooLarge: when len(file_bytes) exceeds the configured cap.
        """
        if len(file_bytes) > self._max_size_bytes:
            raise FileTooLarge(
                f"Attachment {len(file_bytes)}B exceeds limit {self._max_size_bytes}B"
            )

        attachment_id = new_id()
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
            await anyio.to_thread.run_sync(
                partial(
                    self._doc_store.put,
                    object_key=_raw_storage_key(thread_id, attachment_id),
                    data=file_bytes,
                    content_type=mime_type.value,
                )
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
            file_bytes = await anyio.to_thread.run_sync(
                self._doc_store.get, _raw_storage_key(thread_id, attachment_id)
            )

            mime = AttachmentMime(claimed.mime_type)

            stage = "pipeline_run"
            result = await self._pipeline.run(
                file_bytes=file_bytes,
                mime_type=mime,
                user_id=claimed.create_user,
                filename=claimed.filename,
            )
            complete_docs = result["complete"]
            simplified_docs = result["simplified"]

            complete_ast_str = self._ast_to_markdown(complete_docs)
            simplified_ast_str = self._ast_to_markdown(simplified_docs)
            char_counts = {
                "complete": len(complete_ast_str),
                "simplified": len(simplified_ast_str),
            }

            stage = "encrypt_ast"
            ast_variants = {
                "complete": self._ast_cipher.encrypt_ast(complete_ast_str),
                "simplified": self._ast_cipher.encrypt_ast(simplified_ast_str),
            }

            stage = "store_artifacts"
            for variant, encrypted in ast_variants.items():
                key = f"attachments/{thread_id}/{attachment_id}/ast-{variant}"
                await anyio.to_thread.run_sync(
                    partial(
                        self._doc_store.put,
                        object_key=key,
                        data=json.dumps(encrypted).encode("utf-8"),
                        content_type="application/json",
                    )
                )

            stage = "repo_add_artifact"
            content_type = ARTIFACT_CONTENT_TYPE[mime]
            for variant in ast_variants:
                key = f"attachments/{thread_id}/{attachment_id}/ast-{variant}"
                await self._repo.add_artifact(
                    attachment_id=attachment_id,
                    variant=variant,
                    storage_key=key,
                    content_type=content_type,
                    char_count=char_counts[variant],
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

    async def delete(self, attachment_id: str, create_user: str | None = None) -> bool:
        """Delete an attachment's storage objects and DB rows.

        Returns False when the row is missing or not owned by create_user
        (caller maps that to 404) — never raises for a missing/foreign row.
        """
        row = await self._repo.get(attachment_id, create_user=create_user)
        if row is None:
            return False

        await self._delete_row(row)
        return True

    async def delete_by_thread(self, thread_id: str) -> None:
        """Cascade-delete every attachment in a thread (e.g. on session delete).

        No create_user filter — the whole session is going away regardless
        of who uploaded what. Fail-soft per attachment: one bad row must not
        block cleanup of the rest.
        """
        attachments = await self._repo.list_by_thread(thread_id, limit=1000)
        for row in attachments:
            try:
                await self._delete_row(row)
            except Exception as exc:
                logger.error(
                    "chat_attachment.delete_by_thread_failed",
                    attachment_id=row.attachment_id,
                    thread_id=thread_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    async def _delete_row(self, row: AttachmentRow) -> None:
        """Delete one already-fetched row's storage objects + DB rows.

        Storage-delete failures are logged and skipped (fail-soft): a stale
        S3 object must not block the row from being removed.
        """
        artifacts = await self._repo.get_artifacts(row.attachment_id)
        storage_keys = [_raw_storage_key(row.thread_id, row.attachment_id)]
        storage_keys.extend(artifact.storage_key for artifact in artifacts)
        for key in storage_keys:
            try:
                await anyio.to_thread.run_sync(self._doc_store.delete, key)
            except Exception as exc:
                logger.warning(
                    "chat_attachment.delete_storage_failed",
                    attachment_id=row.attachment_id,
                    storage_key=key,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        await self._repo.delete(row.attachment_id)
        logger.info(
            "chat_attachment.deleted", attachment_id=row.attachment_id, thread_id=row.thread_id
        )

    @staticmethod
    def _ast_to_markdown(docs: list) -> str:
        """Convert list of AST Documents to markdown string."""
        lines = []
        for i, doc in enumerate(docs, 1):
            lines.append(f"[{i}] {doc.content}")
        return "\n".join(lines)
