"""T-CAT.10 — ChatAttachmentPipeline: load → unprotect → AST build."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from haystack.dataclasses import Document

from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
from ragent.schemas.attachments import AttachmentMime, UNPROTECT_MIMES


class TestChatAttachmentPipeline:
    """Attachment pipeline: load → optional unprotect → AST build."""

    @pytest.mark.asyncio
    async def test_pipeline_loads_text_plain_without_unprotect(self):
        """Text-only MIME types skip unprotect (no external call)."""
        unprotect_client = AsyncMock()
        pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

        file_bytes = b"hello world"
        result = await pipeline.run(
            file_bytes=file_bytes, mime_type=AttachmentMime.TEXT_PLAIN
        )

        assert "complete" in result
        assert "simplified" in result
        unprotect_client.unprotect.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_loads_text_markdown_without_unprotect(self):
        """Markdown is text-only, skips unprotect."""
        unprotect_client = AsyncMock()
        pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

        file_bytes = b"# Title\nContent here"
        result = await pipeline.run(
            file_bytes=file_bytes, mime_type=AttachmentMime.TEXT_MARKDOWN
        )

        assert "complete" in result
        assert "simplified" in result
        unprotect_client.unprotect.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_loads_text_html_without_unprotect(self):
        """HTML is text-only, skips unprotect."""
        unprotect_client = AsyncMock()
        pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

        file_bytes = b"<html><body>test</body></html>"
        result = await pipeline.run(
            file_bytes=file_bytes, mime_type=AttachmentMime.TEXT_HTML
        )

        assert "complete" in result
        assert "simplified" in result
        unprotect_client.unprotect.assert_not_called()

    @pytest.mark.parametrize("mime_type", [AttachmentMime.PDF, AttachmentMime.DOCX, AttachmentMime.PPTX])
    @pytest.mark.asyncio
    async def test_pipeline_calls_unprotect_for_protected_mimes(self, mime_type: AttachmentMime):
        """Binary MIME types (PDF/DOCX/PPTX) trigger unprotect before AST building."""
        unprotect_client = AsyncMock()
        unprotect_client.unprotect.return_value = b"unprotected content"

        with patch("ragent.pipelines.chat_attachment.pipeline._MimeAwareSplitter") as mock_splitter_class:
            mock_splitter = MagicMock()
            mock_splitter_class.return_value = mock_splitter
            mock_splitter.run.return_value = {"documents": [Document(content="test")]}

            pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

            file_bytes = b"binary content"
            result = await pipeline.run(file_bytes=file_bytes, mime_type=mime_type)

            assert "complete" in result
            assert "simplified" in result
            unprotect_client.unprotect.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipeline_returns_complete_ast_as_list_of_documents(self):
        """complete variant is a list of Haystack Documents (full AST)."""
        unprotect_client = AsyncMock()
        pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

        file_bytes = b"line 1\nline 2"
        result = await pipeline.run(
            file_bytes=file_bytes, mime_type=AttachmentMime.TEXT_PLAIN
        )

        complete = result["complete"]
        assert isinstance(complete, list)
        assert all(isinstance(doc, Document) for doc in complete)

    @pytest.mark.asyncio
    async def test_pipeline_simplified_derived_from_complete(self):
        """simplified is derived in memory from complete (single parse)."""
        unprotect_client = AsyncMock()
        pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

        file_bytes = b"line 1\nline 2\nline 3"
        result = await pipeline.run(
            file_bytes=file_bytes, mime_type=AttachmentMime.TEXT_PLAIN
        )

        complete = result["complete"]
        simplified = result["simplified"]

        assert isinstance(simplified, list)
        assert all(isinstance(doc, Document) for doc in simplified)
        assert len(simplified) <= len(complete), "simplified is subset/derivative of complete"

    @pytest.mark.asyncio
    async def test_pipeline_handles_all_attachment_mime_types(self):
        """Pipeline handles all 6 AttachmentMime values."""
        unprotect_client = AsyncMock()
        unprotect_client.unprotect.return_value = b"unprotected"

        with patch("ragent.pipelines.chat_attachment.pipeline._MimeAwareSplitter") as mock_splitter_class:
            mock_splitter = MagicMock()
            mock_splitter_class.return_value = mock_splitter
            mock_splitter.run.return_value = {"documents": [Document(content="test")]}

            pipeline = ChatAttachmentPipeline(unprotect_client=unprotect_client)

            for mime in AttachmentMime:
                file_bytes = b"test content"
                result = await pipeline.run(file_bytes=file_bytes, mime_type=mime)

                assert "complete" in result
                assert "simplified" in result
                assert isinstance(result["complete"], list)
                assert isinstance(result["simplified"], list)
