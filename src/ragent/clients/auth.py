"""T4.2 — TokenManager: J1→J2 single-flight refresh with 5-min boundary (S9, P-F)."""

import threading
import time
from collections.abc import Callable
from typing import Any


class TokenManager:
    _REFRESH_MARGIN = 300  # refresh 5 minutes before expiry

    def __init__(
        self,
        auth_url: str,
        client_id: str,
        client_secret: str,
        http: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = auth_url.rstrip("/") + "/auth/api/accesstoken"
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - self._REFRESH_MARGIN:
                return self._token
            self._token = self._refresh()
            return self._token

    def _refresh(self) -> str:
        try:
            resp = self._http.post(
                self._url,
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError("Token refresh failed") from exc
        self._expires_at = data["expiresAt"] / 1000.0
        return data["access_token"]
