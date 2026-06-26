"""T-CAT.13 — DocumentArtifactResolver: decrypt ASTs from storage into chat context."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

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
    ):
        self._doc_store = document_store
        self._ast_cipher = ast_cipher
        self._repo = attachment_repository

    async def resolve(self, attachment_ids: list[str]) -> str | None:
        """Resolve attachment IDs to a formatted <attachments> block.

        Args:
            attachment_ids: List of attachment IDs to resolve

        Returns:
            JSON string of attachment metadata array, or None if empty list
        """
        if not attachment_ids:
            return None

        attachments: list[dict[str, Any]] = []

        for att_id in attachment_ids:
            # Fetch attachment metadata
            att_meta = await self._repo.get(att_id)
            if not att_meta:
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
                # Prefer simplified, fallback to complete
                artifact_by_type = {a.ast_type: a for a in artifacts}
                selected = artifact_by_type.get("simplified") or artifact_by_type.get("complete")

                if selected:
                    try:
                        encrypted_data = self._doc_store.get(selected.storage_key)
                        encrypted_obj = json.loads(encrypted_data.decode("utf-8"))
                        decrypted = self._ast_cipher.decrypt_ast(encrypted_obj)
                        if decrypted and "content" in decrypted:
                            att_info["ast"] = decrypted["content"]
                    except (ValueError, KeyError, json.JSONDecodeError) as e:
                        logger.warning(f"Failed to decrypt AST for {att_id}: {e}")

            attachments.append(att_info)

        if not attachments:
            return None

        return json.dumps(attachments)
