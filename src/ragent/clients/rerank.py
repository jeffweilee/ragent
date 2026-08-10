"""T4.8 — RerankClient: bge-reranker-base, top_k param (B-Phase B).

**No application-level retry** (T-RETRY.1). A failed call raises its typed
upstream error immediately; only connection establishment is retried, by the
shared httpx transport in `bootstrap/composition.py`. Rerank is a fail-open
pipeline stage, so surfacing the failure now lets the caller degrade now —
the old 3×@2s loop just made it degrade 94 s later, having tripled the load on
an upstream that was already failing.
"""

import os
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import trace

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import classify_upstream_error
from ragent.utility.env import float_env_or

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
        auth_header_name: str | None = None,
    ) -> None:
        self._url = api_url
        self._http = http
        self._get_token = get_token
        self._timeout = float_env_or(timeout, "RERANK_TIMEOUT_SECONDS", 30.0)
        self._auth_header_name = auth_header_name or os.environ.get(
            "RERANK_AUTH_HEADER_NAME", "Authorization"
        )

    def rerank(self, query: str, texts: list[str], top_k: int = 2) -> list[dict]:
        with _tracer.start_as_current_span("rerank.score") as span:
            span.set_attribute("peer.service", "rerank")
            span.set_attribute("candidate_count", len(texts))
            span.set_attribute("top_k", top_k)
            try:
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
            except Exception as exc:
                span.record_exception(exc)
                error_code, exc_cls = classify_upstream_error(
                    exc,
                    error_code=HttpErrorCode.RERANK_ERROR,
                    timeout_code=HttpErrorCode.RERANK_TIMEOUT,
                )
                logger.error(
                    "rerank.error",
                    peer_service="rerank",
                    candidate_count=len(texts),
                    error_type=type(exc).__name__,
                    error_code=error_code,
                )
                raise exc_cls(
                    f"rerank failed: {exc}",
                    service="rerank",
                    error_code=error_code,
                ) from exc
            logger.info(
                "rerank.call",
                peer_service="rerank",
                candidate_count=len(texts),
                top_k=top_k,
                result_count=len(results),
            )
            return results
