"""T-CAT.13 — DocumentArtifactResolver: decrypt ASTs from storage into chat context."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import structlog

from ragent.security.ast_cipher import ASTDecryptionError

logger = structlog.get_logger(__name__)

# Spec default; composition.py reads ATTACHMENT_ARTIFACT_MAX_CHARS env and
# passes the runtime value here — mirrors ATTACHMENT_MAX_SIZE_BYTES's split
# in chat_attachment_service.py.
ARTIFACT_MAX_CHARS_DEFAULT = 10_000

if TYPE_CHECKING:
    from ragent.repositories.attachment_repository import AttachmentRepository
    from ragent.security.ast_cipher import ASTCipher
    from ragent.storage.document_store import DocumentStore


class DocumentArtifactResolver:
    """Resolve encrypted ASTs from storage for inclusion in chat context.

    Retrieves attachment metadata, fetches encrypted AST artifacts from
    DocumentStore, decrypts them, and formats the result for inclusion in
    the chat context preamble.
    """

    def __init__(
        self,
        document_store: DocumentStore,
        ast_cipher: ASTCipher,
        attachment_repository: AttachmentRepository,
        artifact_max_chars: int = ARTIFACT_MAX_CHARS_DEFAULT,
    ):
        self._doc_store = document_store
        self._ast_cipher = ast_cipher
        self._repo = attachment_repository
        self._artifact_max_chars = artifact_max_chars

    async def resolve(self, attachment_ids: list[str]) -> str | None:
        """Resolve attachment IDs to a formatted <attachments> block.

        Args:
            attachment_ids: List of attachment IDs to resolve

        Returns:
            JSON string of attachment metadata array, or None if empty list
        """
        if not attachment_ids:
            return None

        logger.info(
            "document_artifact_resolver.resolve_started",
            attachment_count=len(attachment_ids),
        )

        attachments: list[dict[str, Any]] = []

        for att_id in attachment_ids:
            # Fetch attachment metadata
            att_meta = await self._repo.get(att_id)
            if not att_meta:
                logger.warning(
                    "document_artifact_resolver.attachment_not_found",
                    attachment_id=att_id,
                )
                continue

            # Format attachment info for context
            att_info = {
                "attachmentId": att_meta.attachment_id,
                "filename": att_meta.filename,
                "mimeType": att_meta.mime_type,
                "sizeBytes": att_meta.size_bytes,
            }

            # Optionally include decrypted AST (simplified variant for context)
            artifacts = await self._repo.get_artifacts(att_id)
            if artifacts:
                # Prefer complete, but fall back to simplified when complete
                # would blow the context-window budget (char_count is
                # computed once at artifact-creation time — no decrypt
                # needed to make this decision).
                artifact_by_variant = {a.variant: a for a in artifacts}
                complete = artifact_by_variant.get("complete")
                simplified = artifact_by_variant.get("simplified")
                selected = (
                    complete
                    if complete and complete.char_count <= self._artifact_max_chars
                    else simplified
                )

                if selected:
                    try:
                        encrypted_data = await anyio.to_thread.run_sync(
                            self._doc_store.get, selected.storage_key
                        )
                        encrypted_obj = json.loads(encrypted_data.decode("utf-8"))
                        att_info["ast"] = self._ast_cipher.decrypt_ast(encrypted_obj)
                    except (ValueError, KeyError, json.JSONDecodeError, ASTDecryptionError) as e:
                        logger.warning(
                            "document_artifact_resolver.decrypt_failed",
                            attachment_id=att_id,
                            error_type=type(e).__name__,
                            error=str(e),
                        )

            attachments.append(att_info)

        if not attachments:
            return None

        logger.info(
            "document_artifact_resolver.resolve_completed",
            resolved_count=len(attachments),
        )
        return json.dumps(attachments)
