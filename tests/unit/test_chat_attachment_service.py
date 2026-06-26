"""T-CAT.11 — chat_attachment_service: validate → store → pipeline → encrypt → persist."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from haystack.dataclasses import Document

from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
from ragent.repositories.attachment_repository import AttachmentRepository
from ragent.schemas.attachments import AttachmentMime
from ragent.security.ast_cipher import ASTCipher
from ragent.services.chat_attachment_service import ChatAttachmentService
from ragent.storage.document_store import DocumentStore


class TestChatAttachmentService:
    """Attachment service orchestration: upload → store → pipeline → encrypt → persist."""

    @pytest.fixture
    def service_dependencies(self):
        """Mock all external dependencies."""
        document_store = MagicMock(spec=DocumentStore)
        ast_cipher = MagicMock(spec=ASTCipher)
        attachment_repository = AsyncMock(spec=AttachmentRepository)
        pipeline = AsyncMock(spec=ChatAttachmentPipeline)

        ast_cipher.encrypt_ast.side_effect = lambda plaintext, **kwargs: {
            "version": "1.0",
            "ciphertext": plaintext[:50],
            "nonce": "deadbeef",
        }

        pipeline.run.return_value = {
            "complete": [Document(content="ast")],
            "simplified": [Document(content="ast")],
        }

        return {
            "document_store": document_store,
            "ast_cipher": ast_cipher,
            "attachment_repository": attachment_repository,
            "pipeline": pipeline,
        }

    def test_service_init_requires_dependencies(self, service_dependencies):
        """Service binds all required dependencies."""
        service = ChatAttachmentService(
            document_store=service_dependencies["document_store"],
            ast_cipher=service_dependencies["ast_cipher"],
            attachment_repository=service_dependencies["attachment_repository"],
            pipeline=service_dependencies["pipeline"],
        )
        assert service is not None

    @pytest.mark.asyncio
    async def test_upload_stores_raw_bytes_and_returns_attachment_id(self, service_dependencies):
        """Upload stores raw bytes to DocumentStore and returns attachment_id."""
        service = ChatAttachmentService(**service_dependencies)

        file_bytes = b"test file content"
        filename = "test.txt"
        thread_id = "thread-1"
        create_user = "alice"
        mime_type = AttachmentMime.TEXT_PLAIN

        attachment_id = await service.upload(
            file_bytes=file_bytes,
            filename=filename,
            thread_id=thread_id,
            create_user=create_user,
            mime_type=mime_type,
        )

        assert attachment_id is not None
        assert len(attachment_id) > 0
        assert service_dependencies["document_store"].put.call_count == 3

    @pytest.mark.asyncio
    async def test_upload_calls_pipeline_with_decoded_content(self, service_dependencies):
        """Upload runs ChatAttachmentPipeline on decoded file content."""
        service_dependencies["pipeline"].run.return_value = {
            "complete": [Document(content="ast complete")],
            "simplified": [Document(content="ast simplified")],
        }

        service = ChatAttachmentService(**service_dependencies)

        file_bytes = b"test content"
        await service.upload(
            file_bytes=file_bytes,
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        service_dependencies["pipeline"].run.assert_called_once()
        call_args = service_dependencies["pipeline"].run.call_args
        assert call_args.kwargs["file_bytes"] == file_bytes
        assert call_args.kwargs["mime_type"] == AttachmentMime.TEXT_PLAIN

    @pytest.mark.asyncio
    async def test_upload_encrypts_both_ast_variants(self, service_dependencies):
        """Upload encrypts complete and simplified AST variants."""
        service_dependencies["pipeline"].run.return_value = {
            "complete": [Document(content="complete ast")],
            "simplified": [Document(content="simplified ast")],
        }

        service = ChatAttachmentService(**service_dependencies)

        await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        assert service_dependencies["ast_cipher"].encrypt_ast.call_count == 2

    @pytest.mark.asyncio
    async def test_upload_stores_artifacts_and_writes_repository(self, service_dependencies):
        """Upload stores encrypted artifacts and writes to attachment_repository."""
        service_dependencies["pipeline"].run.return_value = {
            "complete": [Document(content="c")],
            "simplified": [Document(content="s")],
        }

        service = ChatAttachmentService(**service_dependencies)

        await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        service_dependencies["document_store"].put.assert_called()
        service_dependencies["attachment_repository"].create.assert_called_once()
        service_dependencies["attachment_repository"].add_artifact.assert_called()

    @pytest.mark.asyncio
    async def test_upload_sets_correct_initial_status(self, service_dependencies):
        """Upload promotes attachment status to READY after successful processing.

        AttachmentRepository.create() has no status param (always inserts
        UPLOADED); the service must promote via update_status() instead.
        """
        service_dependencies["pipeline"].run.return_value = {
            "complete": [Document(content="ast")],
            "simplified": [Document(content="ast")],
        }

        service = ChatAttachmentService(**service_dependencies)

        attachment_id = await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        create_call = service_dependencies["attachment_repository"].create.call_args
        assert "status" not in create_call.kwargs
        service_dependencies["attachment_repository"].update_status.assert_called_once_with(
            attachment_id, "READY"
        )

    @pytest.mark.asyncio
    async def test_upload_logs_started_and_completed(self, service_dependencies):
        """Upload emits chat_attachment.upload_started then upload_completed."""
        service = ChatAttachmentService(**service_dependencies)

        with structlog.testing.capture_logs() as logs:
            attachment_id = await service.upload(
                file_bytes=b"test",
                filename="test.txt",
                thread_id="thread-1",
                create_user="alice",
                mime_type=AttachmentMime.TEXT_PLAIN,
            )

        events = [e["event"] for e in logs]
        assert "chat_attachment.upload_started" in events
        assert "chat_attachment.upload_completed" in events

        started = next(e for e in logs if e["event"] == "chat_attachment.upload_started")
        assert started["thread_id"] == "thread-1"
        assert started["filename"] == "test.txt"
        assert started["size_bytes"] == 4

        completed = next(e for e in logs if e["event"] == "chat_attachment.upload_completed")
        assert completed["attachment_id"] == attachment_id
        assert completed["thread_id"] == "thread-1"

    @pytest.mark.asyncio
    async def test_upload_logs_failure_and_reraises(self, service_dependencies):
        """Upload logs chat_attachment.upload_failed and re-raises on pipeline error."""
        service_dependencies["pipeline"].run.side_effect = RuntimeError("pipeline boom")
        service = ChatAttachmentService(**service_dependencies)

        with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError):
            await service.upload(
                file_bytes=b"test",
                filename="test.txt",
                thread_id="thread-1",
                create_user="alice",
                mime_type=AttachmentMime.TEXT_PLAIN,
            )

        failed = next(e for e in logs if e["event"] == "chat_attachment.upload_failed")
        assert failed["thread_id"] == "thread-1"
        assert failed["stage"] == "pipeline_run"
        assert failed["error_type"] == "RuntimeError"
        assert failed["log_level"] == "error"
