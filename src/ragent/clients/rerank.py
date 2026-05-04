"""T4.8 — RerankClient: bge-reranker-base, top_k param (P2 wired)."""

import os
from collections.abc import Callable
from typing import Any


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
        resp = self._http.post(
            self._url,
            json={"model": "bge-reranker-base", "query": query, "texts": texts, "top_k": top_k},
            headers={"Authorization": f"Bearer {self._get_token()}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["results"]
