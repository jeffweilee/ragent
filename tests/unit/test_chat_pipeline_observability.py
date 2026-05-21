"""T-APL.6 — Per-step structured logging for the retrieve pipeline components.

Each retrieval pipeline component emits ``retrieve.step.{started,ok,failed}``
via ``structlog.get_logger("ragent.retrieve")``; the wrapper inherits context
vars bound by ``RequestLoggingMiddleware`` (``request_id``, ``user_id``).
The generic ``wrap_pipeline_component`` helper supports any namespace; this
file tests both the retrieve-pipeline wiring and generic namespace behaviour.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest
import structlog
from haystack.dataclasses import Document
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

from ragent.pipelines.observability import wrap_pipeline_component
from ragent.pipelines.retrieve import (
    _FeedbackMemoryRetriever,
    build_retrieval_pipeline,
    run_retrieval,
)


class _FakeComponent:
    def run(self, documents: list) -> dict:
        return {"documents": [Document() for _ in range(len(documents) + 1)]}


def test_wrap_pipeline_component_emits_namespaced_events() -> None:
    comp = _FakeComponent()
    wrap_pipeline_component(comp, namespace="chat", step="reranker")
    with structlog.testing.capture_logs() as logs:
        comp.run(documents=[Document(), Document(), Document()])
    events = [e for e in logs if e.get("event") in {"chat.step.started", "chat.step.ok"}]
    assert [e["event"] for e in events] == ["chat.step.started", "chat.step.ok"]
    ok = events[1]
    assert ok["step"] == "reranker"
    assert isinstance(ok["duration_ms"], int)
    assert ok["duration_ms"] >= 0
    assert ok["atoms_in"] == 3
    assert ok["chunks_out"] == 4


def test_wrap_pipeline_component_failure_emits_failed_with_namespace() -> None:
    class _BoomComponent:
        def run(self) -> dict:
            raise RuntimeError("boom")

    comp = _BoomComponent()
    wrap_pipeline_component(comp, namespace="chat", step="reranker", error_code="RERANK_ERROR")
    with structlog.testing.capture_logs() as logs, contextlib.suppress(RuntimeError):
        comp.run()
    failed = [e for e in logs if e.get("event") == "chat.step.failed"]
    assert len(failed) == 1
    e = failed[0]
    assert e["step"] == "reranker"
    assert e["error_code"] == "RERANK_ERROR"
    assert "boom" in e["error"]
    assert isinstance(e["duration_ms"], int)


def test_wrap_pipeline_component_ingest_namespace_still_emits_ingest_events() -> None:
    """The generic helper preserves the `ingest.step.*` shape ingest callers depend on."""
    comp = _FakeComponent()
    wrap_pipeline_component(comp, namespace="ingest", step="splitter")
    with structlog.testing.capture_logs() as logs:
        comp.run(documents=[Document()])
    events = [e for e in logs if e.get("event") in {"ingest.step.started", "ingest.step.ok"}]
    assert [e["event"] for e in events] == ["ingest.step.started", "ingest.step.ok"]


@pytest.mark.parametrize(
    ("join_mode", "with_rerank", "with_feedback"),
    [
        ("bm25_only", False, False),
        ("vector_only", False, False),
        ("rrf", False, False),
        ("rrf", True, False),
        ("concatenate", False, False),
        ("rrf", True, True),
    ],
)
def test_build_retrieval_pipeline_wraps_every_component_across_join_modes(
    join_mode: str, with_rerank: bool, with_feedback: bool
) -> None:
    """Every Haystack component in every supported join_mode is wrapped.

    `functools.wraps` on the monkey-patched `run` sets `__wrapped__` to the
    original method; presence of that attribute on every node's `run` proves
    no factory branch silently skipped the `_add` helper.
    """
    rerank_client = MagicMock() if with_rerank else None
    feedback_retriever = (
        _FeedbackMemoryRetriever(es_client=MagicMock(), doc_repo=MagicMock())
        if with_feedback
        else None
    )

    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode=join_mode,
        rerank_client=rerank_client,
        feedback_retriever=feedback_retriever,
    )
    for name in pipeline.graph.nodes:
        node = pipeline.get_component(name)
        assert hasattr(node.run, "__wrapped__"), f"component {name!r} not wrapped"


def test_wrap_pipeline_component_emits_otel_span_per_call(otel_exporter) -> None:
    """Each wrapped run() opens an OTEL span named `{namespace}.step.{step}`."""
    comp = _FakeComponent()
    wrap_pipeline_component(comp, namespace="chat", step="reranker")
    comp.run(documents=[1, 2])
    spans = [s for s in otel_exporter.get_finished_spans() if s.name == "chat.step.reranker"]
    assert len(spans) == 1


def test_wrap_pipeline_component_otel_span_records_failure(otel_exporter) -> None:
    """Span status is ERROR on exception and the exception is recorded."""
    from opentelemetry.trace import StatusCode

    class _BoomComponent:
        def run(self) -> dict:
            raise RuntimeError("boom")

    comp = _BoomComponent()
    wrap_pipeline_component(comp, namespace="chat", step="reranker", error_code="RERANK_ERROR")
    with contextlib.suppress(RuntimeError):
        comp.run()
    spans = [s for s in otel_exporter.get_finished_spans() if s.name == "chat.step.reranker"]
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_count_documents_only_counts_actual_document_lists() -> None:
    """atoms_in must not be set for embedding vectors, int lists, or list-of-lists.

    Misleading atoms_in=1024 (embedding dim) or atoms_in=2 (number of joiner
    inputs) produced nonsense log output and masked real document counts.
    """
    from ragent.pipelines.observability import _count_documents

    # Legitimate document lists → counted.
    assert _count_documents([Document(), Document(), Document()]) == 3
    assert _count_documents([]) == 0
    # Embedding-vector list (floats) → None, not 1024.
    assert _count_documents([0.1] * 1024) is None
    # list-of-lists from DocumentJoiner → None, not number-of-streams.
    assert _count_documents([[Document()], [Document()]]) is None
    # Dicts (ingest pipeline stubs) → None.
    assert _count_documents([{"a": 1}, {"b": 2}]) is None
    # Integers → None.
    assert _count_documents([1, 2, 3, 4]) is None


def test_build_retrieval_pipeline_wraps_each_component_with_retrieve_namespace() -> None:
    """Every component the factory adds emits a `retrieve.step.ok` on a happy-path run.

    Uses join_mode=bm25_only to minimise the mock surface: only the BM25
    retriever needs a fake doc-producing run, no embedder is constructed.
    """
    # Docs WITHOUT document_id meta keep the hydrator's anyio.from_thread.run path
    # cold — sync unit tests aren't inside an AnyIO worker thread, and the
    # hydrator's docstring is explicit about that pre-existing constraint.
    bm25_docs = [Document(id="A", content="alpha", meta={})]

    store = MagicMock(spec=ElasticsearchDocumentStore)
    store._bm25_retrieval.return_value = bm25_docs

    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=store,
        doc_repo=MagicMock(),
        join_mode="bm25_only",
    )

    with structlog.testing.capture_logs() as logs:
        run_retrieval(pipeline, query="alpha", top_k=1)

    ok_events = {e["step"] for e in logs if e.get("event") == "retrieve.step.ok"}
    assert {"bm25_retriever", "source_hydrator", "excerpt_truncator"}.issubset(ok_events)
    # Must NOT emit chat.step.ok — SRP: retrieve endpoint must not pollute chat namespace.
    assert not any(e.get("event") == "chat.step.ok" for e in logs)


def test_wrap_pipeline_component_emits_debug_docs_on_document_output() -> None:
    """After a successful run returning documents, a DEBUG `.docs` event is emitted
    with {document_id, chunk_id, score} for each output document."""

    class _PassthroughComponent:
        def run(self, documents: list) -> dict:
            return {"documents": documents}

    comp = _PassthroughComponent()
    wrap_pipeline_component(comp, namespace="retrieve", step="hydrator")
    doc = Document(id="chunk-1", content="hello", meta={"document_id": "doc-A"}, score=0.92)

    with structlog.testing.capture_logs() as logs:
        comp.run(documents=[doc])

    debug_events = [e for e in logs if e.get("event") == "retrieve.step.ok.docs"]
    assert len(debug_events) == 1
    entries = debug_events[0]["doc_refs"]
    assert entries == [{"document_id": "doc-A", "chunk_id": "chunk-1", "score": 0.92}]
