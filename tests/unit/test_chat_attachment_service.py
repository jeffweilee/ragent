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
from ragent.services.chat_attachment_service import ChatAttachmentService, FileTooLarge
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
        assert len(attachment_id) == 26
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
    async def test_upload_runs_document_store_put_off_the_event_loop(
        self, service_dependencies, monkeypatch
    ):
        """DocumentStore.put is blocking sync I/O; upload() must offload it via
        anyio.to_thread.run_sync instead of calling it directly on the event loop."""
        import ragent.services.chat_attachment_service as cas_module

        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append(fn)
            return fn(*args, **kwargs)

        monkeypatch.setattr(cas_module.anyio.to_thread, "run_sync", fake_run_sync)
        service = ChatAttachmentService(**service_dependencies)

        await service.upload(
            file_bytes=b"test",
            filename="test.txt",
            thread_id="thread-1",
            create_user="alice",
            mime_type=AttachmentMime.TEXT_PLAIN,
        )

        assert len(run_sync_calls) == 1
        service_dependencies["document_store"].put.assert_called_once()

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

    @pytest.mark.asyncio
    async def test_upload_raises_file_too_large_over_configured_cap(self, service_dependencies):
        """Upload raises FileTooLarge when file_bytes exceeds max_size_bytes,
        without writing to the document store or repository (mirrors
        ingest_service.py's authoritative post-read size check)."""
        service = ChatAttachmentService(**service_dependencies, max_size_bytes=10)

        with pytest.raises(FileTooLarge):
            await service.upload(
                file_bytes=b"this file is way over ten bytes",
                filename="test.txt",
                thread_id="thread-1",
                create_user="alice",
                mime_type=AttachmentMime.TEXT_PLAIN,
            )

        service_dependencies["document_store"].put.assert_not_called()
        service_dependencies["attachment_repository"].create.assert_not_called()

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
    async def test_process_runs_document_store_io_off_the_event_loop(
        self, service_dependencies, monkeypatch
    ):
        """DocumentStore.get/put are blocking sync I/O; process() must offload every
        call (1 raw-bytes fetch + 2 artifact stores) via anyio.to_thread.run_sync."""
        import ragent.services.chat_attachment_service as cas_module

        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append(fn)
            return fn(*args, **kwargs)

        monkeypatch.setattr(cas_module.anyio.to_thread, "run_sync", fake_run_sync)
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        assert len(run_sync_calls) == 3
        service_dependencies["document_store"].get.assert_called_once()
        assert service_dependencies["document_store"].put.call_count == 2

    @pytest.mark.asyncio
    async def test_process_passes_artifact_content_type_to_repository(self, service_dependencies):
        """content_type is resolved from ARTIFACT_CONTENT_TYPE and passed to add_artifact."""
        service = ChatAttachmentService(**service_dependencies)

        await service.process("ATT001")

        calls = service_dependencies["attachment_repository"].add_artifact.call_args_list
        assert len(calls) == 2
        assert all(call.kwargs["content_type"] == "text/markdown" for call in calls)

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

    # ------------------------------------------------------------------
    # delete() — single attachment + its artifacts
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_removes_storage_objects_and_db_row(self, service_dependencies):
        """Delete fetches the row+artifacts, deletes every storage key, then repo.delete."""
        from ragent.repositories.attachment_repository import ArtifactRow

        service_dependencies["attachment_repository"].get.return_value = _claimed_row()
        service_dependencies["attachment_repository"].get_artifacts.return_value = [
            ArtifactRow(
                attachment_id="ATT001",
                variant="complete",
                storage_key="attachments/thread-1/ATT001/ast-complete",
                content_type="text/markdown",
                created_at=None,
            ),
            ArtifactRow(
                attachment_id="ATT001",
                variant="simplified",
                storage_key="attachments/thread-1/ATT001/ast-simplified",
                content_type="text/markdown",
                created_at=None,
            ),
        ]
        service = ChatAttachmentService(**service_dependencies)

        result = await service.delete("ATT001", create_user="alice")

        assert result is True
        deleted_keys = {
            call.args[0] for call in service_dependencies["document_store"].delete.call_args_list
        }
        assert deleted_keys == {
            "attachments/thread-1/ATT001/raw",
            "attachments/thread-1/ATT001/ast-complete",
            "attachments/thread-1/ATT001/ast-simplified",
        }
        service_dependencies["attachment_repository"].delete.assert_called_once_with("ATT001")

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_row_missing_or_not_owned(self, service_dependencies):
        """Delete returns False (no exception) when repo.get yields None — missing or IDOR."""
        service_dependencies["attachment_repository"].get.return_value = None
        service = ChatAttachmentService(**service_dependencies)

        result = await service.delete("ATT001", create_user="alice")

        assert result is False
        service_dependencies["attachment_repository"].delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_is_fail_soft_on_storage_delete_error(self, service_dependencies):
        """A stale/missing S3 object must not block the DB row from being removed."""
        service_dependencies["attachment_repository"].get.return_value = _claimed_row()
        service_dependencies["attachment_repository"].get_artifacts.return_value = []
        service_dependencies["document_store"].delete.side_effect = RuntimeError("not found")
        service = ChatAttachmentService(**service_dependencies)

        result = await service.delete("ATT001")

        assert result is True
        service_dependencies["attachment_repository"].delete.assert_called_once_with("ATT001")

    # ------------------------------------------------------------------
    # delete_by_thread() — cascade delete on session delete
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_by_thread_deletes_every_attachment_in_thread(
        self, service_dependencies
    ):
        """delete_by_thread lists (no create_user filter) then deletes each row
        without re-fetching it via repo.get (avoids an N+1 SELECT per row)."""
        rows = [_claimed_row(attachment_id="ATT001"), _claimed_row(attachment_id="ATT002")]
        service_dependencies["attachment_repository"].list_by_thread.return_value = rows
        service_dependencies["attachment_repository"].get_artifacts.return_value = []
        service = ChatAttachmentService(**service_dependencies)

        await service.delete_by_thread("thread-1")

        service_dependencies["attachment_repository"].list_by_thread.assert_called_once_with(
            "thread-1", limit=1000
        )
        assert service_dependencies["attachment_repository"].delete.call_count == 2
        service_dependencies["attachment_repository"].get.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_by_thread_is_fail_soft_per_attachment(self, service_dependencies):
        """One attachment's delete failing must not block deleting the rest."""
        rows = [_claimed_row(attachment_id="ATT001"), _claimed_row(attachment_id="ATT002")]
        service_dependencies["attachment_repository"].list_by_thread.return_value = rows
        service_dependencies["attachment_repository"].get_artifacts.side_effect = RuntimeError(
            "db down"
        )
        service = ChatAttachmentService(**service_dependencies)

        with structlog.testing.capture_logs() as logs:
            await service.delete_by_thread("thread-1")

        events = [e["event"] for e in logs]
        assert events.count("chat_attachment.delete_by_thread_failed") == 2
