"""T-CAT.13 — document_artifact_resolver: decrypt ASTs for chat context."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from ragent.repositories.attachment_repository import (
    ArtifactRow,
    AttachmentRepository,
    AttachmentRow,
)
from ragent.security.ast_cipher import ASTCipher
from ragent.services.document_artifact_resolver import DocumentArtifactResolver
from ragent.storage.document_store import DocumentStore

_NOW = datetime.datetime(2026, 1, 1)


def _attachment_row(
    attachment_id: str, filename: str, mime_type: str, size_bytes: int
) -> AttachmentRow:
    return AttachmentRow(
        attachment_id=attachment_id,
        thread_id="thread-1",
        create_user="alice",
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        status="READY",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _artifact_row(attachment_id: str, ast_type: str, storage_key: str) -> ArtifactRow:
    return ArtifactRow(
        attachment_id=attachment_id, ast_type=ast_type, storage_key=storage_key, created_at=_NOW
    )


class TestDocumentArtifactResolver:
    """Resolve and decrypt attachment artifacts for chat context."""

    @pytest.fixture
    def resolver_dependencies(self):
        """Mock external dependencies."""
        document_store = MagicMock(spec=DocumentStore)
        ast_cipher = MagicMock(spec=ASTCipher)
        attachment_repository = AsyncMock(spec=AttachmentRepository)

        ast_cipher.decrypt_ast.side_effect = lambda ciphertext_obj, **kwargs: {
            "content": f"Decrypted: {ciphertext_obj.get('ciphertext', '')[:20]}..."
        }

        return {
            "document_store": document_store,
            "ast_cipher": ast_cipher,
            "attachment_repository": attachment_repository,
        }

    def test_resolver_init_requires_dependencies(self, resolver_dependencies):
        """Resolver binds all required dependencies."""
        resolver = DocumentArtifactResolver(
            document_store=resolver_dependencies["document_store"],
            ast_cipher=resolver_dependencies["ast_cipher"],
            attachment_repository=resolver_dependencies["attachment_repository"],
        )
        assert resolver is not None

    @pytest.mark.asyncio
    async def test_resolve_empty_attachment_ids_returns_none(self, resolver_dependencies):
        """Resolve with no attachment IDs returns None."""
        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve([])

        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_single_attachment_retrieves_and_decrypts(self, resolver_dependencies):
        """Resolve retrieves attachment metadata and decrypts AST."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )

        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "attachments/t1/att_1/ast-complete")
        ]

        resolver_dependencies[
            "document_store"
        ].get.return_value = b'{"ciphertext":"xyz","nonce":"abc"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_1"])

        assert result is not None
        assert isinstance(result, str)
        assert "att_1" in result or "test.pdf" in result

    @pytest.mark.asyncio
    async def test_resolve_multiple_attachments(self, resolver_dependencies):
        """Resolve handles multiple attachment IDs."""
        resolver_dependencies["attachment_repository"].get.side_effect = [
            _attachment_row("att_1", "doc1.txt", "text/plain", 100),
            _attachment_row("att_2", "doc2.txt", "text/plain", 200),
        ]

        resolver_dependencies["attachment_repository"].get_artifacts.side_effect = [
            [_artifact_row("att_1", "complete", "key1")],
            [_artifact_row("att_2", "complete", "key2")],
        ]

        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_1", "att_2"])

        assert result is not None
        assert "att_1" in result or "doc1.txt" in result
        assert "att_2" in result or "doc2.txt" in result

    @pytest.mark.asyncio
    async def test_resolve_formats_as_json_array(self, resolver_dependencies):
        """Resolve returns a JSON array of attachment info."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_123", "report.pdf", "application/pdf", 5000
        )

        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_123", "complete", "key")
        ]

        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_123"])

        # Should be valid JSON that can be parsed
        import json

        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) > 0

    @pytest.mark.asyncio
    async def test_resolve_logs_started_and_completed(self, resolver_dependencies):
        """Resolve emits resolve_started and resolve_completed business-step logs."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        with structlog.testing.capture_logs() as logs:
            await resolver.resolve(["att_1"])

        started = next(
            e for e in logs if e["event"] == "document_artifact_resolver.resolve_started"
        )
        assert started["attachment_count"] == 1
        completed = next(
            e for e in logs if e["event"] == "document_artifact_resolver.resolve_completed"
        )
        assert completed["resolved_count"] == 1

    @pytest.mark.asyncio
    async def test_resolve_logs_attachment_not_found(self, resolver_dependencies):
        """Resolve logs a warning and skips when the attachment row is missing."""
        resolver_dependencies["attachment_repository"].get.return_value = None

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        with structlog.testing.capture_logs() as logs:
            result = await resolver.resolve(["att_missing"])

        assert result is None
        missing = next(
            e for e in logs if e["event"] == "document_artifact_resolver.attachment_not_found"
        )
        assert missing["attachment_id"] == "att_missing"
        assert missing["log_level"] == "warning"

    @pytest.mark.asyncio
    async def test_resolve_logs_decrypt_failure(self, resolver_dependencies):
        """Resolve logs a structured warning when AST decryption fails."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies["document_store"].get.return_value = b"not-json"

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        with structlog.testing.capture_logs() as logs:
            result = await resolver.resolve(["att_1"])

        assert result is not None
        failed = next(e for e in logs if e["event"] == "document_artifact_resolver.decrypt_failed")
        assert failed["attachment_id"] == "att_1"
        assert failed["log_level"] == "warning"
        assert "error_type" in failed
