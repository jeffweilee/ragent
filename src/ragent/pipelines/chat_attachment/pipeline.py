"""T-CAT.10 — ChatAttachmentPipeline: load → unprotect → AST build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from haystack.dataclasses import Document

from ragent.pipelines.ingest.splitter import _MimeAwareSplitter
from ragent.schemas.attachments import AttachmentMime, UNPROTECT_MIMES

if TYPE_CHECKING:
    from ragent.clients.unprotect_client import UnprotectClient


class ChatAttachmentPipeline:
    """Load attachment file → optional unprotect → build AST.

    Returns both complete and simplified variants (currently identical;
    simplification strategy to be implemented in a future task).
    """

    def __init__(self, unprotect_client: UnprotectClient):
        self._unprotect_client = unprotect_client
        self._splitter = _MimeAwareSplitter()

    async def run(
        self, file_bytes: bytes, mime_type: AttachmentMime
    ) -> dict[str, list[Document]]:
        """Run the attachment pipeline: load → unprotect → AST build.

        Args:
            file_bytes: Raw file content as bytes
            mime_type: AttachmentMime type of the file

        Returns:
            dict with "complete" and "simplified" keys, each containing list[Document]
        """
        content_bytes = file_bytes

        if mime_type in UNPROTECT_MIMES:
            content_bytes = await self._unprotect_client.unprotect(
                file_bytes, mime_type=mime_type.value
            )

        content_str = content_bytes.decode("utf-8")
        doc = Document(content=content_str, meta={"mime_type": mime_type.value})

        atoms = self._splitter.run([doc])["documents"]

        return {
            "complete": atoms,
            "simplified": atoms,
        }
