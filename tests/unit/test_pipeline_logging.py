"""T2v.42 — Per-step structured logging for ingest pipeline components.

Each pipeline component emits ``ingest.step.{started,ok,failed}`` via
``structlog.get_logger("ragent.ingest")``. Failures map to the existing
error-code taxonomy (``PIPELINE_UNROUTABLE`` / ``EMBEDDER_ERROR`` /
``ES_WRITE_ERROR`` / ``PIPELINE_TIMEOUT``). The worker emits terminal
``ingest.ready`` / ``ingest.failed`` events with totals.
"""

from __future__ import annotations

import contextlib

import structlog

from ragent.pipelines.observability import (
    IngestStepError,
    bind_ingest_context,
    log_ingest_step,
    wrap_component_run,
)

# ---------------------------------------------------------------------------
# wrap_component_run — happy path
# ---------------------------------------------------------------------------


class _FakeComponent:
    def __init__(self) -> None:
        self.called_with: dict | None = None

    def run(self, documents: list, **kwargs) -> dict:
        self.called_with = {"documents": documents, **kwargs}
        return {"documents": [{"out": True} for _ in range(2)]}


def test_wrap_emits_started_and_ok_with_expected_fields() -> None:
    comp = _FakeComponent()
    wrap_component_run(comp, step="embedder")
    with (
        structlog.testing.capture_logs() as logs,
        bind_ingest_context(document_id="DOC-1", mime_type="text/markdown"),
    ):
        out = comp.run(documents=[{"a": 1}, {"a": 2}, {"a": 3}])
    assert out == {"documents": [{"out": True}, {"out": True}]}

    events = [e for e in logs if e.get("event", "").startswith("ingest.step.")]
    assert [e["event"] for e in events] == ["ingest.step.started", "ingest.step.ok"]
    started, ok = events
    assert started["step"] == "embedder"
    assert started["document_id"] == "DOC-1"
    assert started["mime_type"] == "text/markdown"
    assert ok["step"] == "embedder"
    assert ok["document_id"] == "DOC-1"
    assert ok["mime_type"] == "text/markdown"
    assert isinstance(ok["duration_ms"], int)
    assert ok["duration_ms"] >= 0
    assert ok["atoms_in"] == 3
    assert ok["chunks_out"] == 2


# ---------------------------------------------------------------------------
# wrap_component_run — failure path
# ---------------------------------------------------------------------------


class _BoomComponent:
    def run(self, documents: list) -> dict:
        raise RuntimeError("boom")


def test_wrap_emits_failed_with_error_code_and_reraises() -> None:
    comp = _BoomComponent()
    wrap_component_run(comp, step="embedder", error_code="EMBEDDER_ERROR")
    with (
        structlog.testing.capture_logs() as logs,
        bind_ingest_context(document_id="DOC-2", mime_type="text/plain"),
    ):
        try:
            comp.run(documents=[1, 2])
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:  # pragma: no cover
            raise AssertionError("expected RuntimeError")

    failed = [e for e in logs if e.get("event") == "ingest.step.failed"]
    assert len(failed) == 1
    e = failed[0]
    assert e["step"] == "embedder"
    assert e["document_id"] == "DOC-2"
    assert e["mime_type"] == "text/plain"
    assert e["error_code"] == "EMBEDDER_ERROR"
    assert "boom" in e["error"]
    assert isinstance(e["duration_ms"], int)


def test_wrap_failure_with_explicit_error_code_via_exception() -> None:
    """Components that raise ``IngestStepError`` carry their own error_code."""

    class _RouterRaiser:
        def run(self, documents: list) -> dict:
            raise IngestStepError("unroutable mime", error_code="PIPELINE_UNROUTABLE")

    comp = _RouterRaiser()
    wrap_component_run(comp, step="router", error_code="PIPELINE_UNROUTABLE")
    with (
        structlog.testing.capture_logs() as logs,
        bind_ingest_context(document_id="DOC-3", mime_type="text/x-bogus"),
        contextlib.suppress(IngestStepError),
    ):
        comp.run(documents=[])

    failed = [e for e in logs if e.get("event") == "ingest.step.failed"]
    assert failed[0]["error_code"] == "PIPELINE_UNROUTABLE"


# ---------------------------------------------------------------------------
# Pipeline-level ordering: build_ingest_pipeline wraps each step
# ---------------------------------------------------------------------------


def test_build_ingest_pipeline_wraps_steps_in_order(monkeypatch) -> None:
    """Run the v1 pipeline end-to-end with mocks; assert step events emitted in order."""
    from unittest.mock import MagicMock

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

    from ragent.pipelines.factory import DocumentEmbedder, build_ingest_pipeline

    class _StubEmbedder:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = DocumentEmbedder(_StubEmbedder())
    document_store = MagicMock(spec=ElasticsearchDocumentStore)
    document_store.write_documents.return_value = 0

    pipe = build_ingest_pipeline(embedder=embedder, document_store=document_store)

    import tempfile

    with (
        structlog.testing.capture_logs() as logs,
        bind_ingest_context(document_id="DOC-PIPE", mime_type="text/plain"),
    ):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("hello world. this is a sentence. and another.")
            path = f.name
        pipe.run({"converter": {"sources": [path]}})

    step_events = [e for e in logs if e.get("event", "").startswith("ingest.step.")]
    # Each component yields a started then ok event in order.
    pairs: list[tuple[str, str]] = []
    for ev in step_events:
        pairs.append((ev["event"].split(".")[-1], ev["step"]))
    # Strip duplicates from any retries; keep the first occurrence for each step.
    started_steps = [s for k, s in pairs if k == "started"]
    ok_steps = [s for k, s in pairs if k == "ok"]
    # Must contain in order: convert → clean → chunker → embedder → writer.
    expected = ["convert", "clean", "chunker", "embedder", "writer"]
    assert started_steps == expected
    assert ok_steps == expected
    for ev in step_events:
        assert ev["document_id"] == "DOC-PIPE"
        assert ev["mime_type"] == "text/plain"


# ---------------------------------------------------------------------------
# log_ingest_step terminal helpers
# ---------------------------------------------------------------------------


def test_wrap_writer_chunks_out_uses_int_documents_written() -> None:
    """Haystack DocumentWriter.run returns {"documents_written": int}."""

    class _IntWriter:
        def run(self, documents: list) -> dict:
            return {"documents_written": len(documents)}

    comp = _IntWriter()
    wrap_component_run(comp, step="writer", error_code="ES_WRITE_ERROR")
    with (
        structlog.testing.capture_logs() as logs,
        bind_ingest_context(document_id="DOC-W", mime_type="text/plain"),
    ):
        comp.run(documents=[1, 2, 3, 4])

    ok = [e for e in logs if e.get("event") == "ingest.step.ok"][0]
    assert ok["chunks_out"] == 4
    assert ok["atoms_in"] == 4


def test_log_ingest_step_ready_emits_terminal_event() -> None:
    with structlog.testing.capture_logs() as logs:
        log_ingest_step.ready(document_id="DOC-9", chunks_total=7, duration_ms_total=42)
    e = [x for x in logs if x.get("event") == "ingest.ready"][0]
    assert e["document_id"] == "DOC-9"
    assert e["chunks_total"] == 7
    assert e["duration_ms_total"] == 42


def test_log_ingest_step_failed_emits_terminal_event() -> None:
    with structlog.testing.capture_logs() as logs:
        log_ingest_step.failed(
            document_id="DOC-9", reason="pipeline_error", error_code="EMBEDDER_ERROR"
        )
    e = [x for x in logs if x.get("event") == "ingest.failed"][0]
    assert e["document_id"] == "DOC-9"
    assert e["reason"] == "pipeline_error"
    assert e["error_code"] == "EMBEDDER_ERROR"
