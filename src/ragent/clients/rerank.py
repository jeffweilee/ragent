"""T4.8 — RerankClient: bge-reranker-base, top_k param, retry 3×@2s (B-Phase B)."""

import os
import time as _time
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import trace

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import classify_upstream_error

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)

_SUCCESS_CODE = 96200


class RerankClient:
    def __init__(
        self,
        api_url: str,
        http: Any,
        get_token: Callable[[], str],
        timeout: float | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        auth_header_name: str | None = None,
    ) -> None:
        self._url = api_url
        self._http = http
        self._get_token = get_token
        self._timeout = timeout or float(os.environ.get("RERANK_TIMEOUT_SECONDS", "30"))
        self._sleep = sleep
        self._auth_header_name = auth_header_name or os.environ.get(
            "RERANK_AUTH_HEADER_NAME", "Authorization"
        )

    def rerank(self, query: str, texts: list[str], top_k: int = 2) -> list[dict]:
        with _tracer.start_as_current_span("rerank.score") as span:
            span.set_attribute("peer.service", "rerank")
            span.set_attribute("candidate_count", len(texts))
            span.set_attribute("top_k", top_k)
            last_exc: Exception | None = None
            for attempt in range(3):
                if attempt:
                    self._sleep(2.0)
                try:
                    span.set_attribute("retry_attempt", attempt)
                    resp = self._http.post(
                        self._url,
                        json={
                            "model": "bge-reranker-base",
                            "question": query,
                            "documents": texts,
                            "top_k": top_k,
                        },
                        headers={self._auth_header_name: self._get_token()},
                        timeout=self._timeout,
                    )
                    span.set_attribute("http.status_code", getattr(resp, "status_code", 0))
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("returnCode") != _SUCCESS_CODE:
                        raise ValueError(
                            f"Unexpected returnCode: {data.get('returnCode')}. "
                            f"Message: {data.get('returnMessage')}"
                        )
                    results = data["returnData"]
                    logger.info(
                        "rerank.call",
                        peer_service="rerank",
                        candidate_count=len(texts),
                        top_k=top_k,
                        result_count=len(results),
                        retry_attempt=attempt,
                    )
                    return results
                except Exception as exc:
                    last_exc = exc
            span.record_exception(last_exc)  # type: ignore[arg-type]
            error_code, exc_cls = classify_upstream_error(
                last_exc,
                error_code=HttpErrorCode.RERANK_ERROR,
                timeout_code=HttpErrorCode.RERANK_TIMEOUT,
            )
            logger.error(
                "rerank.error",
                peer_service="rerank",
                candidate_count=len(texts),
                error_type=type(last_exc).__name__ if last_exc else None,
                error_code=error_code,
            )
            raise exc_cls(
                f"rerank failed after retries: {last_exc}",
                service="rerank",
                error_code=error_code,
            ) from last_exc
