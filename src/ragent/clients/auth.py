"""T4.2 — TokenManager: J1→J2 single-flight refresh with 5-min boundary (S9, P-F).

Spec: docs/00_rule.md §"LLM & Embedding & Re-rank Auth API (Token Exchange)"
- POST {"key": j1_token} → {"token": j2, "expiresAt": "2026-01-07T13:20:36Z"}
- Local mode:  j1_token supplied directly (from AI_LLM/EMBEDDING/RERANK_API_J1_TOKEN)
- K8s mode:    j1_token=None + k8s_sa_token_path reads file each refresh
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class TokenManager:
    _REFRESH_MARGIN = 300  # refresh 5 minutes before expiry

    def __init__(
        self,
        auth_url: str,
        http: Any,
        j1_token: str | None = None,
        k8s_sa_token_path: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if j1_token is None and k8s_sa_token_path is None:
            raise ValueError("Either j1_token or k8s_sa_token_path must be provided")
        self._url = auth_url.rstrip("/") + "/auth/api/accesstoken"
        self._j1_token = j1_token
        self._k8s_path = k8s_sa_token_path
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _get_j1(self) -> str:
        if self._j1_token is not None:
            return self._j1_token
        # K8s mode: read fresh from SA file each refresh
        try:
            with open(self._k8s_path) as f:  # type: ignore[arg-type]
                return f.read().strip()
        except OSError as exc:
            raise RuntimeError("Token refresh failed") from exc

    def get_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - self._REFRESH_MARGIN:
                return self._token
            self._token = self._refresh()
            return self._token

    def _refresh(self) -> str:
        j1 = self._get_j1()
        try:
            resp = self._http.post(self._url, json={"key": j1})
            resp.raise_for_status()
            data = resp.json()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Token refresh failed") from exc
        dt = datetime.strptime(data["expiresAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self._expires_at = dt.timestamp()
        return data["token"]
