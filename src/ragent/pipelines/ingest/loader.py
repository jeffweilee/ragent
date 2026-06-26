"""_TextLoader and ALLOWED_MIMES for the v2 ingest pipeline."""

from __future__ import annotations

from typing import Any

from haystack.core.component import component
from haystack.dataclasses import Document

from ragent.schemas.ingest import IngestMime

ALLOWED_MIMES = (
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    IngestMime.PDF,
)

# ---------------------------------------------------------------------------
# _TextLoader (T2v.30/31)
# ---------------------------------------------------------------------------


@component
class _TextLoader:
    """Build a single ``Document`` from inline content + per-document meta.

    The worker calls ``run(content=..., mime_type=..., document_id=...)`` so
    the loader replaces v1's ``TextFileToDocument`` + tempfile dance.
    """

    @component.output_types(documents=list[Document])
    def run(
        self,
        content: str,
        mime_type: str,
        document_id: str | None = None,
        source_url: str | None = None,
        source_title: str | None = None,
        source_app: str | None = None,
        source_meta: str | None = None,
        content_bytes: bytes | None = None,
    ) -> dict:
        meta: dict[str, Any] = {"mime_type": mime_type}
        for k, v in (
            ("document_id", document_id),
            ("source_url", source_url),
            ("source_title", source_title),
            ("source_app", source_app),
            ("source_meta", source_meta),
        ):
            if v is not None:
                meta[k] = v
        if content_bytes is not None:
            meta["raw_bytes"] = content_bytes
        return {"documents": [Document(content=content, meta=meta)]}
