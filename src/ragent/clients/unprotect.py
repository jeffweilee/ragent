"""T-UP.3 — UnprotectClient: obtain raw binary from external unprotect API."""

from __future__ import annotations

import os

import httpx


class UnprotectClient:
    def __init__(
        self,
        api_url: str,
        apikey: str,
        delegated_user_suffix: str,
        http: httpx.Client,
        timeout: float | None = None,
    ) -> None:
        self._api_url = api_url
        self._apikey = apikey
        self._suffix = delegated_user_suffix
        self._http = http
        self._timeout = timeout or float(os.environ.get("UNPROTECT_TIMEOUT_SECONDS", "30"))

    def unprotect(self, file_bytes: bytes, user_id: str, filename: str) -> bytes:
        response = self._http.post(
            self._api_url,
            headers={"apikey": self._apikey},
            files={"fileInput": (filename, file_bytes)},
            data={"delegatedUser": f"{user_id}{self._suffix}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.content
