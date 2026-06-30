"""T-CAT.13 — document_artifact_resolver: decrypt ASTs for chat context."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

import ragent.services.document_artifact_resolver as resolver_module
from ragent.repositories.attachment_repository import (
    ArtifactRow,
    AttachmentRepository,
    AttachmentRow,
)
from ragent.security.ast_cipher import ASTCipher, ASTDecryptionError
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


def _artifact_row(
    attachment_id: str, variant: str, storage_key: str, char_count: int = 100
) -> ArtifactRow:
    return ArtifactRow(
        attachment_id=attachment_id,
        variant=variant,
        storage_key=storage_key,
        content_type="text/markdown",
        char_count=char_count,
        created_at=_NOW,
    )


class TestTruncate:
    """Direct unit tests for the `_truncate` helper (PR #208 review)."""

    def test_returns_text_unchanged_when_under_cap(self):
        assert resolver_module._truncate("hello", 10) == "hello"

    def test_returns_none_when_budget_already_exhausted(self):
        assert resolver_module._truncate("hello", 0) is None

    def test_truncated_output_never_exceeds_max_chars(self):
        """The marker is reserved inside max_chars, so output length is always
        <= max_chars — never longer than the original it replaces (PR #208 Codex
        review: the old slice-then-append approach could exceed the cap)."""
        marker_len = len(resolver_module._TRUNCATION_MARKER)
        text = "x" * (50 + marker_len)  # just over the boundary

        result = resolver_module._truncate(text, 50)

        assert result == "x" * (50 - marker_len) + resolver_module._TRUNCATION_MARKER
        assert len(result) == 50

    def test_truncates_and_appends_marker_once_genuinely_over_budget(self):
        marker_len = len(resolver_module._TRUNCATION_MARKER)
        text = "x" * (50 + marker_len + 1)  # one char past the boundary

        result = resolver_module._truncate(text, 50)

        assert result == "x" * (50 - marker_len) + resolver_module._TRUNCATION_MARKER
        assert len(result) == 50

    def test_omits_marker_when_max_chars_too_small_to_fit_it(self):
        marker_len = len(resolver_module._TRUNCATION_MARKER)
        text = "x" * (marker_len + 10)

        result = resolver_module._truncate(text, marker_len - 1)

        assert result == "x" * (marker_len - 1)


class TestDocumentArtifactResolver:
    """Resolve and decrypt attachment artifacts for chat context."""

    @pytest.fixture
    def resolver_dependencies(self):
        """Mock external dependencies."""
        document_store = MagicMock(spec=DocumentStore)
        ast_cipher = MagicMock(spec=ASTCipher)
        attachment_repository = AsyncMock(spec=AttachmentRepository)

        ast_cipher.decrypt_ast.side_effect = lambda ciphertext_obj, **kwargs: (
            f"Decrypted: {ciphertext_obj.get('ciphertext', '')[:20]}..."
        )

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
    async def test_resolve_prefers_complete_when_under_char_limit(self, resolver_dependencies):
        """complete is selected (fetched from storage) when its char_count fits the budget."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key-complete", char_count=500),
            _artifact_row("att_1", "simplified", "key-simplified", char_count=50),
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=1_000)

        await resolver.resolve(["att_1"])

        resolver_dependencies["document_store"].get.assert_called_once_with("key-complete")

    @pytest.mark.asyncio
    async def test_resolve_includes_selected_variant_in_att_info(self, resolver_dependencies):
        """att_info records which variant ("complete"/"simplified") was selected."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key-complete", char_count=500),
            _artifact_row("att_1", "simplified", "key-simplified", char_count=50),
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=1_000)

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert parsed[0]["variant"] == "complete"

    @pytest.mark.asyncio
    async def test_resolve_includes_simplified_variant_when_fallback_selected(
        self, resolver_dependencies
    ):
        """att_info records "simplified" when complete exceeds the char budget."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key-complete", char_count=5_000),
            _artifact_row("att_1", "simplified", "key-simplified", char_count=50),
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=1_000)

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert parsed[0]["variant"] == "simplified"

    @pytest.mark.asyncio
    async def test_resolve_omits_variant_when_no_artifacts(self, resolver_dependencies):
        """att_info has no "variant" key when the attachment has no artifacts yet."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = []

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert "variant" not in parsed[0]

    @pytest.mark.asyncio
    async def test_resolve_falls_back_to_simplified_when_complete_exceeds_char_limit(
        self, resolver_dependencies
    ):
        """complete is skipped in favour of simplified when char_count exceeds the budget."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key-complete", char_count=5_000),
            _artifact_row("att_1", "simplified", "key-simplified", char_count=50),
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=1_000)

        await resolver.resolve(["att_1"])

        resolver_dependencies["document_store"].get.assert_called_once_with("key-simplified")

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

    @pytest.mark.asyncio
    async def test_resolve_dispatches_doc_store_get_via_run_sync(
        self, resolver_dependencies, monkeypatch
    ):
        """resolve() must never call the blocking DocumentStore.get() directly on the
        event loop — it must go through anyio.to_thread.run_sync(), matching the
        convention established for every other DocumentStore call site (T-CAT.W10)."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append(fn)
            return fn(*args, **kwargs)

        monkeypatch.setattr(resolver_module.anyio.to_thread, "run_sync", fake_run_sync)

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        await resolver.resolve(["att_1"])

        assert run_sync_calls == [resolver_dependencies["document_store"].get]

    @pytest.mark.asyncio
    async def test_resolve_handles_ast_decryption_error(self, resolver_dependencies):
        """Resolve catches ASTDecryptionError (tampered/wrong-key ciphertext) without raising."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies[
            "document_store"
        ].get.return_value = b'{"ciphertext":"xyz","nonce":"abc"}'
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = ASTDecryptionError(
            "failed to decrypt AST: bad tag"
        )

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        with structlog.testing.capture_logs() as logs:
            result = await resolver.resolve(["att_1"])

        assert result is not None
        import json

        parsed = json.loads(result)
        assert "content" not in parsed[0]
        assert parsed[0]["variant"] == "complete"
        failed = next(e for e in logs if e["event"] == "document_artifact_resolver.decrypt_failed")
        assert failed["attachment_id"] == "att_1"
        assert failed["log_level"] == "warning"
        assert failed["error_type"] == "ASTDecryptionError"

    @pytest.mark.asyncio
    async def test_resolve_includes_decrypted_content_field(self, resolver_dependencies):
        """att_info["content"] carries the decrypted text on the success path."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies[
            "document_store"
        ].get.return_value = b'{"ciphertext":"xyz","nonce":"abc"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert parsed[0]["content"] == "Decrypted: xyz..."

    @pytest.mark.asyncio
    async def test_resolve_does_not_escape_non_ascii_content(self, resolver_dependencies):
        """json.dumps uses ensure_ascii=False — CJK stays literal, no \\uXXXX escapes."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "報告.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key")
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "中文內容測試"
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"xyz"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        result = await resolver.resolve(["att_1"])

        assert "\\u" not in result
        assert "報告.pdf" in result
        assert "中文內容測試" in result

    @pytest.mark.asyncio
    async def test_resolve_truncates_simplified_variant_exceeding_artifact_max_chars(
        self, resolver_dependencies
    ):
        """The simplified fallback is now capped too — previously unbounded."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "simplified", "key-simplified", char_count=50)
        ]
        big_text = "x" * 200
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: big_text
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=100)

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert len(parsed[0]["content"]) == 100
        assert parsed[0]["content"].endswith(resolver_module._TRUNCATION_MARKER)

    @pytest.mark.asyncio
    async def test_resolve_appends_marker_once_when_both_caps_trigger(self, resolver_dependencies):
        """When the per-attachment cap and the per-turn budget both bind on the
        same attachment, only one truncation marker is ever appended."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key-complete", char_count=50)
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "x" * 200
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(
            **resolver_dependencies, artifact_max_chars=100, total_max_chars=30
        )

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        content = parsed[0]["content"]
        marker_len = len(resolver_module._TRUNCATION_MARKER)
        assert content == "x" * (30 - marker_len) + resolver_module._TRUNCATION_MARKER
        assert len(content) == 30
        assert content.count(resolver_module._TRUNCATION_MARKER.strip()) == 1

    @pytest.mark.asyncio
    async def test_resolve_omits_content_when_total_budget_is_zero(self, resolver_dependencies):
        """A single attachment against a zero total budget gets no content at all."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "doc1.txt", "text/plain", 100
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key1", char_count=80)
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "y" * 80
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(
            **resolver_dependencies, artifact_max_chars=1_000, total_max_chars=0
        )

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        assert "content" not in parsed[0]
        assert parsed[0]["variant"] == "complete"

    @pytest.mark.asyncio
    async def test_resolve_truncates_complete_variant_under_total_budget(
        self, resolver_dependencies
    ):
        """`complete` passes the per-attachment cap but still gets clipped by the
        per-turn aggregate budget."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "doc1.txt", "text/plain", 100
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key1", char_count=50)
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "z" * 80
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(
            **resolver_dependencies, artifact_max_chars=1_000, total_max_chars=30
        )

        result = await resolver.resolve(["att_1"])

        import json

        parsed = json.loads(result)
        marker_len = len(resolver_module._TRUNCATION_MARKER)
        assert parsed[0]["content"] == "z" * (30 - marker_len) + resolver_module._TRUNCATION_MARKER
        assert parsed[0]["variant"] == "complete"

    @pytest.mark.asyncio
    async def test_resolve_omits_content_once_total_budget_exhausted(self, resolver_dependencies):
        """2nd attachment loses content once the per-turn total budget is spent;
        both still carry full metadata + variant."""
        resolver_dependencies["attachment_repository"].get.side_effect = [
            _attachment_row("att_1", "doc1.txt", "text/plain", 100),
            _attachment_row("att_2", "doc2.txt", "text/plain", 200),
        ]
        resolver_dependencies["attachment_repository"].get_artifacts.side_effect = [
            [_artifact_row("att_1", "complete", "key1", char_count=80)],
            [_artifact_row("att_2", "complete", "key2", char_count=80)],
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "y" * 80
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(
            **resolver_dependencies, artifact_max_chars=1_000, total_max_chars=80
        )

        result = await resolver.resolve(["att_1", "att_2"])

        import json

        parsed = json.loads(result)
        assert parsed[0]["content"] == "y" * 80
        assert "content" not in parsed[1]
        assert parsed[0]["variant"] == "complete"
        assert parsed[1]["variant"] == "complete"
        assert parsed[1]["attachmentId"] == "att_2"

    @pytest.mark.asyncio
    async def test_resolve_skips_fetch_and_decrypt_once_total_budget_exhausted(
        self, resolver_dependencies
    ):
        """Once the aggregate budget is spent, later attachments must not pay for a
        storage fetch + decrypt that will be discarded (PR #208 Codex review)."""
        resolver_dependencies["attachment_repository"].get.side_effect = [
            _attachment_row("att_1", "doc1.txt", "text/plain", 100),
            _attachment_row("att_2", "doc2.txt", "text/plain", 200),
        ]
        resolver_dependencies["attachment_repository"].get_artifacts.side_effect = [
            [_artifact_row("att_1", "complete", "key1", char_count=80)],
            [_artifact_row("att_2", "complete", "key2", char_count=80)],
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "y" * 80
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(
            **resolver_dependencies, artifact_max_chars=1_000, total_max_chars=80
        )

        await resolver.resolve(["att_1", "att_2"])

        assert resolver_dependencies["document_store"].get.call_count == 1
        assert resolver_dependencies["ast_cipher"].decrypt_ast.call_count == 1

    @pytest.mark.asyncio
    async def test_resolve_logs_truncation_warning(self, resolver_dependencies):
        """A truncation (per-attachment or per-turn) emits attachment_content_truncated."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "test.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "key", char_count=50)
        ]
        resolver_dependencies["ast_cipher"].decrypt_ast.side_effect = (
            lambda ciphertext_obj, **kwargs: "x" * 200
        )
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies, artifact_max_chars=100)

        with structlog.testing.capture_logs() as logs:
            await resolver.resolve(["att_1"])

        truncated = next(
            e
            for e in logs
            if e["event"] == "document_artifact_resolver.attachment_content_truncated"
        )
        assert truncated["attachment_id"] == "att_1"
        assert truncated["original_chars"] == 200
        assert truncated["kept_chars"] == 100
        assert truncated["log_level"] == "warning"

    @pytest.mark.asyncio
    async def test_resolve_logs_attachment_referenced_audit_fields(self, resolver_dependencies):
        """attachment_referenced carries thread/attachment id, size, variant, char_count,
        artifact_id — and never filename/content/ast."""
        resolver_dependencies["attachment_repository"].get.return_value = _attachment_row(
            "att_1", "secret-name.pdf", "application/pdf", 1024
        )
        resolver_dependencies["attachment_repository"].get_artifacts.return_value = [
            _artifact_row("att_1", "complete", "attachments/thread-1/att_1/ast-complete", 42)
        ]
        resolver_dependencies["document_store"].get.return_value = b'{"ciphertext":"data"}'

        resolver = DocumentArtifactResolver(**resolver_dependencies)

        with structlog.testing.capture_logs() as logs:
            await resolver.resolve(["att_1"])

        referenced = next(
            e for e in logs if e["event"] == "document_artifact_resolver.attachment_referenced"
        )
        assert referenced["thread_id"] == "thread-1"
        assert referenced["attachment_id"] == "att_1"
        assert referenced["size_bytes"] == 1024
        assert referenced["variant"] == "complete"
        assert referenced["char_count"] == 42
        assert referenced["artifact_id"] == "attachments/thread-1/att_1/ast-complete"

        for event in logs:
            if event["event"] == "document_artifact_resolver.attachment_referenced":
                assert "filename" not in event
                assert "content" not in event
                assert "ast" not in event
