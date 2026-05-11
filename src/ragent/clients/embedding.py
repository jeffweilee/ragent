"""T4.4 — EmbeddingClient: bge-m3, batch=32, retry 3×@1s, asymmetric timeouts (P-B, C8)."""

import math
import os
import time as _time
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import trace

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import classify_upstream_error

_EMBED_MODEL = "bge-m3"
_SUCCESS_CODE = 96200
logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _validate_vectors(vectors: list[list[float]]) -> None:
    """Reject NaN/Inf/zero-magnitude vectors before they reach ES.

    ES dense_vector cosine indices silently reject zero-magnitude writes
    with a `magnitude_zero` error that is hard to trace back; NaN/Inf
    poisons downstream similarity scoring. Raise to trigger a retry.
    """
    for i, v in enumerate(vectors):
        if not isinstance(v, list) or not v:
            raise ValueError(f"embedding {i} is not a non-empty list: {type(v).__name__}")
        sq = 0.0
        for x in v:
            if not isinstance(x, (int, float)) or not math.isfinite(x):
                raise ValueError(f"embedding {i} contains non-finite component")
            sq += x * x
        if sq == 0.0:
            raise ValueError(f"embedding {i} has zero magnitude")


class EmbeddingClient:
    def __init__(
        self,
        api_url: str,
        http: Any,
        get_token: Callable[[], str],
        batch_size: int | None = None,
        ingest_timeout: float | None = None,
        query_timeout: float | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        auth_header_name: str | None = None,
    ) -> None:
        self._url = api_url
        self._http = http
        self._get_token = get_token
        self._batch_size = batch_size or int(os.environ.get("EMBEDDER_BATCH_SIZE", "32"))
        self._ingest_timeout = ingest_timeout or float(
            os.environ.get("EMBEDDER_INGEST_TIMEOUT_SECONDS", "30")
        )
        self._query_timeout = query_timeout or float(
            os.environ.get("EMBEDDER_QUERY_TIMEOUT_SECONDS", "10")
        )
        self._sleep = sleep
        self._auth_header_name = auth_header_name or os.environ.get(
            "EMBEDDING_AUTH_HEADER_NAME", "Authorization"
        )

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        timeout = self._query_timeout if query else self._ingest_timeout
        result: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            result.extend(self._call(texts[i : i + self._batch_size], timeout))
        return result

    def _call(self, texts: list[str], timeout: float) -> list[list[float]]:
        with _tracer.start_as_current_span("embedding.embed") as span:
            span.set_attribute("peer.service", "embedding")
            span.set_attribute("batch_size", len(texts))
            last_exc: Exception | None = None
            for attempt in range(3):
                if attempt:
                    self._sleep(1.0)
                try:
                    span.set_attribute("retry_attempt", attempt)
                    resp = self._http.post(
                        self._url,
                        json={"model": _EMBED_MODEL, "texts": texts},
                        headers={self._auth_header_name: self._get_token()},
                        timeout=timeout,
                    )
                    span.set_attribute("http.status_code", getattr(resp, "status_code", 0))
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("returnCode") != _SUCCESS_CODE:
                        raise ValueError(f"Unexpected returnCode: {data.get('returnCode')}")
                    out = [item["embedding"] for item in data["data"]]
                    _validate_vectors(out)
                    if out and isinstance(out[0], list):
                        span.set_attribute("dim", len(out[0]))
                    logger.info(
                        "embedding.call",
                        peer_service="embedding",
                        batch_size=len(texts),
                        retry_attempt=attempt,
                    )
                    return out
                except Exception as exc:
                    last_exc = exc
            span.record_exception(last_exc)  # type: ignore[arg-type]
            error_code, exc_cls = classify_upstream_error(
                last_exc,
                error_code=HttpErrorCode.EMBEDDER_ERROR,
                timeout_code=HttpErrorCode.EMBEDDER_TIMEOUT,
            )
            logger.error(
                "embedding.error",
                peer_service="embedding",
                batch_size=len(texts),
                error_type=type(last_exc).__name__ if last_exc else None,
                error_code=error_code,
            )
            raise exc_cls(
                f"embedding failed after retries: {last_exc}",
                service="embedding",
                error_code=error_code,
            ) from last_exc
