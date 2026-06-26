"""T-CAT.11 / T-CAT.W2 — chat_attachment_service: upload (fast intake) + process (async worker)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from haystack.dataclasses import Document

from ragent.bootstrap.dispatcher import TaskiqDispatcher
from ragent.pipelines.chat_attachment.pipeline import ChatAttachmentPipeline
from ragent.repositories.attachment_repository import AttachmentRepository, AttachmentRow
from ragent.schemas.attachments import AttachmentMime
from ragent.security.ast_cipher import ASTCipher
from ragent.services.chat_attachment_service import ChatAttachmentService
from ragent.storage.document_store import DocumentStore


def _claimed_row(**kwargs) -> AttachmentRow:
    import datetime

    base = dict(
        attachment_id="ATT001",
        thread_id="thread-1",
        create_user="alice",
        filename="test.txt",
        mime_type=AttachmentMime.TEXT_PLAIN.value,
        size_bytes=4,
        status="UPLOADED",
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        updated_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    )
    base.update(kwargs)
    return AttachmentRow(**base)


class TestChatAttachmentService:
    """Attachment service orchestration: upload (fast intake) / process (async worker)."""

    @pytest.fixture
    def service_dependencies(self):
        """Mock all external dependencies."""
        document_store = MagicMock(spec=DocumentStore)
        ast_cipher = MagicMock(spec=ASTCipher)
        attachment_repository = AsyncMock(spec=AttachmentRepository)
        pipeline = AsyncMock(spec=ChatAttachmentPipeline)
        dispatcher = AsyncMock(spec=TaskiqDispatcher)

        ast_cipher.encrypt_ast.side_effect = lambda plaintext, **kwargs: {
            "version": "1.0",
            "ciphertext": plaintext[:50],
            "nonce": "deadbeef",
        }

        pipeline.run.return_value = {
            "complete": [Document(content="ast")],
            "simplified": [Document(content="ast")],
        }

        attachment_repository.claim_for_processing.return_value = _claimed_row()
        document_store.get.return_value = b"test file content"

        return {
            "document_store": document_store,
            "ast_cipher": ast_cipher,
            "attachment_repository": attachment_repository,
            "pipeline": pipeline,
            "dispatcher": dispatcher,
        }

    def test_service_init_requires_dependencies(self, service_dependencies):
        """Service binds all required dependencies."""
        service = ChatAttachmentService(
            document_store=service_dependencies["document_store"],
            ast_cipher=service_dependencies["ast_cipher"],
            attachment_repository=service_dependencies["attachment_repository"],
            pipeline=service_dependencies["pipeline"],
            dispatcher=service_dependencies["dispatcher"],
        )
        assert service is not None

    # ------------------------------------------------------------------
    # upload() — fast intake only
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upload_stores_raw_bytes_and_returns_attachment_id(self, service_dependencies):
        """Upload stores raw bytes to DocumentStore and returns attachment_id."""
        service = ChatAttachmentService(**service_dependencies)

        file_bytes = b"test file content"
        attachment_id = await service.upload(
            file_bytes=file_bytes,
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        assert attachment_id is not None
        assert len(attachment_id) > 0
        service_dependencies["document_store"].put.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_does_not_run_pipeline_or_cipher_synchronously(self, service_dependencies):
        """Upload defers pipeline/encrypt work to the async worker."""
        service = ChatAttachmentService(**service_dependencies)

        await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        service_dependencies["pipeline"].run.assert_not_called()
        service_dependencies["ast_cipher"].encrypt_ast.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_writes_repository_and_dispatches_processing(self, service_dependencies):
        """Upload writes the UPLOADED row and enqueues attachment.process."""
        service = ChatAttachmentService(**service_dependencies)

        attachment_id = await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        service_dependencies["attachment_repository"].create.assert_called_once()
        service_dependencies["dispatcher"].enqueue.assert_called_once_with(
            "attachment.process", attachment_id=attachment_id
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
        """Upload logs chat_attachment.upload_failed and re-raises on enqueue error."""
        service_dependencies["dispatcher"].enqueue.side_effect = RuntimeError("broker down")
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
        assert failed["stage"] == "enqueue_process"
        assert failed["error_type"] == "RuntimeError"
        assert failed["log_level"] == "error"

    # ------------------------------------------------------------------
    # process() — async worker processing
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_process_claims_row_and_promotes_to_ready(self, service_dependencies):
        """Process claims UPLOADED→PROCESSING, runs pipeline+encrypt, promotes to READY."""
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        service_dependencies["attachment_repository"].claim_for_processing.assert_called_once_with(
            "ATT001"
        )
        service_dependencies["pipeline"].run.assert_called_once()
        assert service_dependencies["ast_cipher"].encrypt_ast.call_count == 2
        service_dependencies["attachment_repository"].update_status.assert_called_once_with(
            "ATT001", "READY"
        )

    @pytest.mark.asyncio
    async def test_process_stores_artifacts_and_writes_repository(self, service_dependencies):
        """Process stores encrypted artifacts and writes to attachment_repository."""
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        service_dependencies["document_store"].put.assert_called()
        service_dependencies["attachment_repository"].add_artifact.assert_called()
        assert service_dependencies["attachment_repository"].add_artifact.call_count == 2

    @pytest.mark.asyncio
    async def test_process_no_op_when_claim_returns_none(self, service_dependencies):
        """Process gracefully no-ops (no exception) when the row is already claimed/terminal."""
        service_dependencies["attachment_repository"].claim_for_processing.return_value = None
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        service_dependencies["pipeline"].run.assert_not_called()
        service_dependencies["attachment_repository"].update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_terminalizes_to_failed_on_pipeline_error(self, service_dependencies):
        """Process catches pipeline errors and terminalizes to FAILED with diagnostics."""
        service_dependencies["pipeline"].run.side_effect = RuntimeError("pipeline boom")
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        service_dependencies["attachment_repository"].update_status.assert_called_once_with(
            "ATT001",
            "FAILED",
            error_code="PIPELINE_UNEXPECTED_ERROR",
            error_reason="RuntimeError: pipeline boom",
        )

    @pytest.mark.asyncio
    async def test_process_logs_failure_without_reraising(self, service_dependencies):
        """Process logs chat_attachment.process_failed but does not propagate the exception."""
        service_dependencies["pipeline"].run.side_effect = RuntimeError("pipeline boom")
        service = ChatAttachmentService(**service_dependencies)

        with structlog.testing.capture_logs() as logs:
            await service.process("ATT001")

        failed = next(e for e in logs if e["event"] == "chat_attachment.process_failed")
        assert failed["attachment_id"] == "ATT001"
        assert failed["thread_id"] == "thread-1"
        assert failed["stage"] == "pipeline_run"
        assert failed["error_type"] == "RuntimeError"
        assert failed["log_level"] == "error"

    @pytest.mark.asyncio
    async def test_process_logs_completed_on_success(self, service_dependencies):
        """Process emits chat_attachment.process_completed on success."""
        service = ChatAttachmentService(**service_dependencies)

        with structlog.testing.capture_logs() as logs:
            await service.process("ATT001")

        events = [e["event"] for e in logs]
        assert "chat_attachment.process_completed" in events
