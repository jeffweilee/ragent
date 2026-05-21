"""T2v.42/43 + T-APL.7 — Per-step structured logs for ingest AND chat pipelines.

Components are wrapped at construction time so their public ``run`` signature
is preserved (Haystack 2.x introspects the original via ``functools.wraps``).
Each call emits ``{namespace}.step.{started,ok,failed}`` on the
``ragent.{namespace}`` logger; the wrapper inherits ``structlog.contextvars``
bound by the caller (``document_id``/``mime_type`` on the ingest worker,
``request_id``/``user_id`` from ``RequestLoggingMiddleware`` on chat).
"""

from __future__ import annotations

import contextlib
import functools
import time
from collections.abc import Iterator
from typing import Any

import structlog
from haystack.dataclasses import Document
from opentelemetry import trace

_INGEST_LOGGER = structlog.get_logger("ragent.ingest")
_tracer = trace.get_tracer(__name__)


class IngestStepError(Exception):
    """Raised by pipeline components to surface a stable ``error_code``.

    Wrapped components default to the error code declared on the wrapper;
    raising this lets components override on a per-call basis (e.g. the
    file-type router that knows ``PIPELINE_UNROUTABLE`` is correct).
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@contextlib.contextmanager
def bind_ingest_context(*, document_id: str, mime_type: str | None = None) -> Iterator[None]:
    tokens = structlog.contextvars.bind_contextvars(
        document_id=document_id,
        **({"mime_type": mime_type} if mime_type is not None else {}),
    )
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def _ctx() -> dict[str, Any]:
    return {k: v for k, v in structlog.contextvars.get_contextvars().items() if v is not None}


def _count_documents(value: Any) -> int | None:
    if isinstance(value, list) and (not value or isinstance(value[0], Document)):
        return len(value)
    return None


def wrap_pipeline_component(
    component: Any,
    *,
    namespace: str,
    step: str,
    error_code: str = "PIPELINE_UNEXPECTED_ERROR",
) -> Any:
    """Monkey-patch ``component.run`` to emit ``{namespace}.step.{started,ok,failed}``.

    ``error_code`` is the default code attached to the failure event; components
    can override by raising ``IngestStepError(error_code=...)`` (the exception
    is re-used by the chat pipeline too — the error-code carrier is
    namespace-agnostic, only the event prefix changes).
    """
    original = component.run
    logger = structlog.get_logger(f"ragent.{namespace}")
    started_event = f"{namespace}.step.started"
    ok_event = f"{namespace}.step.ok"
    failed_event = f"{namespace}.step.failed"
    span_name = f"{namespace}.step.{step}"

    @functools.wraps(original)
    def _logged(*args: Any, **kwargs: Any) -> Any:
        ctx = _ctx()
        # Prefer the kwarg literally named `documents` so wrapped components whose
        # first positional / kwarg is a non-document list (e.g. `query_embedding:
        # list[float]` on _DynamicFieldEmbeddingRetriever, or list-of-lists on
        # DocumentJoiner) don't emit a misleading `atoms_in` count.
        atoms_in: int | None = None
        if "documents" in kwargs:
            atoms_in = _count_documents(kwargs["documents"])
        if atoms_in is None:
            for v in list(args) + list(kwargs.values()):
                atoms_in = _count_documents(v)
                if atoms_in is not None:
                    break
        logger.info(started_event, step=step, **ctx)
        started = time.monotonic()
        with _tracer.start_as_current_span(span_name) as span:
            span.set_attribute("pipeline.namespace", namespace)
            span.set_attribute("pipeline.step", step)
            if atoms_in is not None:
                span.set_attribute("atoms_in", atoms_in)
            try:
                result = original(*args, **kwargs)
            except IngestStepError as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, exc.error_code))
                logger.error(
                    failed_event,
                    step=step,
                    duration_ms=duration_ms,
                    error_code=exc.error_code,
                    error=str(exc),
                    **ctx,
                )
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, error_code))
                logger.error(
                    failed_event,
                    step=step,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    error=f"{type(exc).__name__}: {exc}",
                    **ctx,
                )
                raise
            duration_ms = int((time.monotonic() - started) * 1000)
            chunks_out: int | None = None
            if isinstance(result, dict):
                for key in ("documents", "documents_written"):
                    if key in result:
                        val = result[key]
                        chunks_out = val if isinstance(val, int) else _count_documents(val)
                        break
            if chunks_out is not None:
                span.set_attribute("chunks_out", chunks_out)
            span.set_attribute("duration_ms", duration_ms)
            if isinstance(result, dict) and "documents" in result:
                docs_out = result["documents"]
                if _count_documents(docs_out) is not None:
                    logger.info(
                        ok_event + ".docs",
                        step=step,
                        doc_refs=[
                            {
                                "document_id": (d.meta or {}).get("document_id"),
                                "chunk_id": d.id,
                                "score": d.score,
                            }
                            for d in docs_out
                        ],
                        **_ctx(),
                    )
        # Re-snapshot contextvars: components may bind new ones during run()
        # (e.g. _MimeAwareSplitter sets `splitter=<label>`) — the ok payload
        # MUST include those, so do not reuse the pre-run `ctx` here.
        payload: dict[str, Any] = {"step": step, "duration_ms": duration_ms, **_ctx()}
        if atoms_in is not None:
            payload["atoms_in"] = atoms_in
        if chunks_out is not None:
            payload["chunks_out"] = chunks_out
        logger.info(ok_event, **payload)
        return result

    component.run = _logged
    return component


class _TerminalLogger:
    """Worker-terminal events. Kept as a namespace for grep-ability."""

    @staticmethod
    def ready(*, document_id: str, chunks_total: int, duration_ms_total: int) -> None:
        _INGEST_LOGGER.info(
            "ingest.ready",
            document_id=document_id,
            chunks_total=chunks_total,
            duration_ms_total=duration_ms_total,
        )

    @staticmethod
    def failed(*, document_id: str, reason: str, error_code: str) -> None:
        _INGEST_LOGGER.error(
            "ingest.failed",
            document_id=document_id,
            reason=reason,
            error_code=error_code,
        )


log_ingest_step = _TerminalLogger()
