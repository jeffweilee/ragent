"""T4.8 — RerankClient: bge-reranker-base, top_k param (P2 wired)."""

import os
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import trace

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class RerankClient:
    def __init__(
        self,
        api_url: str,
        http: Any,
        get_token: Callable[[], str],
        timeout: float | None = None,
    ) -> None:
        self._url = api_url.rstrip("/")
        self._http = http
        self._get_token = get_token
        self._timeout = timeout or float(os.environ.get("RERANK_TIMEOUT_SECONDS", "30"))

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
                        "query": query,
                        "texts": texts,
                        "top_k": top_k,
                    },
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                    timeout=self._timeout,
                )
                span.set_attribute("http.status_code", getattr(resp, "status_code", 0))
                resp.raise_for_status()
                results = resp.json()["results"]
                logger.info(
                    "rerank.call",
                    peer_service="rerank",
                    candidate_count=len(texts),
                    top_k=top_k,
                    result_count=len(results),
                )
                return results
            except Exception as exc:
                span.record_exception(exc)
                logger.error(
                    "rerank.error",
                    peer_service="rerank",
                    candidate_count=len(texts),
                    error_type=type(exc).__name__,
                )
                raise
