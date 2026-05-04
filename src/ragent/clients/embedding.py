"""T4.4 — EmbeddingClient: bge-m3, batch=32, retry 3×@1s, asymmetric timeouts (P-B, C8)."""

import os
import time as _time
from collections.abc import Callable
from typing import Any

_EMBED_MODEL = "bge-m3"
_SUCCESS_CODE = 96200


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
    ) -> None:
        self._url = api_url.rstrip("/") + "/text_embedding"
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

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        timeout = self._query_timeout if query else self._ingest_timeout
        result: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            result.extend(self._call(texts[i : i + self._batch_size], timeout))
        return result

    def _call(self, texts: list[str], timeout: float) -> list[list[float]]:
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt:
                self._sleep(1.0)
            try:
                resp = self._http.post(
                    self._url,
                    json={"model": _EMBED_MODEL, "texts": texts},
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("returnCode") != _SUCCESS_CODE:
                    raise ValueError(f"Unexpected returnCode: {data.get('returnCode')}")
                return [item["embedding"] for item in data["data"]]
            except Exception as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]
