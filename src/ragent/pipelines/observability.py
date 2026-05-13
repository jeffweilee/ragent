"""T2v.42/43 — Per-step structured logs for the ingest pipeline.

Components are wrapped at construction time so their public ``run`` signature
is preserved (Haystack 2.x introspects the original via ``functools.wraps``).
Each call emits ``ingest.step.{started,ok,failed}`` on the
``ragent.ingest`` logger; ``document_id`` and ``mime_type`` are read from
``structlog.contextvars`` so the worker can bind once at task entry and
every nested component log inherits the context.
"""

from __future__ import annotations

import contextlib
import functools
import time
from collections.abc import Iterator
from typing import Any

import structlog

_logger = structlog.get_logger("ragent.ingest")


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
    if isinstance(value, list):
        return len(value)
    return None


def wrap_component_run(
    component: Any, *, step: str, error_code: str = "PIPELINE_UNEXPECTED_ERROR"
) -> Any:
    """Monkey-patch ``component.run`` to emit per-step events.

    ``error_code`` is the default code attached to ``ingest.step.failed``;
    components can override by raising ``IngestStepError(error_code=...)``.
    """
    original = component.run

    @functools.wraps(original)
    def _logged(*args: Any, **kwargs: Any) -> Any:
        ctx = _ctx()
        atoms_in: int | None = None
        for v in list(args) + list(kwargs.values()):
            atoms_in = _count_documents(v)
            if atoms_in is not None:
                break
        _logger.info("ingest.step.started", step=step, **ctx)
        started = time.monotonic()
        try:
            result = original(*args, **kwargs)
        except IngestStepError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            _logger.error(
                "ingest.step.failed",
                step=step,
                duration_ms=duration_ms,
                error_code=exc.error_code,
                error=str(exc),
                **ctx,
            )
            raise
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            _logger.error(
                "ingest.step.failed",
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
        payload: dict[str, Any] = {"step": step, "duration_ms": duration_ms, **_ctx()}
        if atoms_in is not None:
            payload["atoms_in"] = atoms_in
        if chunks_out is not None:
            payload["chunks_out"] = chunks_out
        _logger.info("ingest.step.ok", **payload)
        return result

    component.run = _logged
    return component


class _TerminalLogger:
    """Worker-terminal events. Kept as a namespace for grep-ability."""

    @staticmethod
    def ready(*, document_id: str, chunks_total: int, duration_ms_total: int) -> None:
        _logger.info(
            "ingest.ready",
            document_id=document_id,
            chunks_total=chunks_total,
            duration_ms_total=duration_ms_total,
        )

    @staticmethod
    def failed(*, document_id: str, reason: str, error_code: str) -> None:
        _logger.error(
            "ingest.failed",
            document_id=document_id,
            reason=reason,
            error_code=error_code,
        )


log_ingest_step = _TerminalLogger()
