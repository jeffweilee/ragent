"""T-CAT.10 — ChatAttachmentPipeline: load → unprotect → AST build."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
from haystack.dataclasses import Document

from ragent.pipelines.ingest.splitter import _MimeAwareSplitter
from ragent.schemas.attachments import UNPROTECT_MIMES, AttachmentMime

if TYPE_CHECKING:
    from ragent.clients.unprotect import UnprotectClient

logger = logging.getLogger(__name__)


class ChatAttachmentPipeline:
    """Load attachment file → optional unprotect → build AST.

    Returns both complete and simplified variants (currently identical;
    simplification strategy to be implemented in a future task).
    """

    def __init__(self, unprotect_client: UnprotectClient | None):
        self._unprotect_client = unprotect_client
        self._splitter = _MimeAwareSplitter()

    async def run(
        self,
        file_bytes: bytes,
        mime_type: AttachmentMime,
        *,
        user_id: str = "anonymous",
        filename: str = "attachment",
    ) -> dict[str, list[Document]]:
        """Run the attachment pipeline: load → unprotect → AST build.

        Args:
            file_bytes: Raw file content as bytes
            mime_type: AttachmentMime type of the file
            user_id: Uploading user, forwarded to the unprotect API (delegated user)
            filename: Original filename, forwarded to the unprotect API

        Returns:
            dict with "complete" and "simplified" keys, each containing list[Document]
        """
        content_bytes = file_bytes

        # Per docs/spec/chat_attachments.md §3: skipped when no unprotect_client is
        # wired, or (fail-soft) when the call raises — original bytes are used as a
        # fallback in both cases. The unprotect API is synchronous network I/O, so it
        # runs off-loop.
        if mime_type in UNPROTECT_MIMES and self._unprotect_client is not None:
            try:
                content_bytes = await anyio.to_thread.run_sync(
                    self._unprotect_client.unprotect, file_bytes, user_id, filename
                )
            except Exception:
                logger.warning("chat_attachment.unprotect_failed_fallback", exc_info=True)

        content_str = content_bytes.decode("utf-8")
        doc = Document(content=content_str, meta={"mime_type": mime_type.value})

        atoms = self._splitter.run([doc])["documents"]

        return {
            "complete": atoms,
            "simplified": atoms,
        }
