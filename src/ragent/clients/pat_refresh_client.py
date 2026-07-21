"""PatRefreshClient — rotates a PAT via the refresh service (T-PAT).

`PUT {PAT_REFRESH_API}` with a service-credential header
(`PAT_API_HEADER_TOKEN_KEY: PAT_API_HEADER_TOKEN_VALUE`) and body
`{"patToken": <current>}` → `{"patToken": <new>}`. The HTTP status is mapped to
a typed error so `PatService` can apply the refresh state machine (§PAT):
401 → re-authorize (no retry), 400 → our bug, 429 → transient (retry), anything
else / transport failure → transient.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class PatRefreshError(Exception):
    """Base for all refresh outcomes other than success."""


class PatRefreshUnauthorized(PatRefreshError):
    """401 — the current PAT can no longer be refreshed (auth gone)."""


class PatRefreshBadRequest(PatRefreshError):
    """400 — malformed request (empty body / PAT); a caller-side bug."""


class PatRefreshRateLimited(PatRefreshError):
    """429 — the refresh service is rate-limiting; retry with backoff."""


class PatRefreshTransient(PatRefreshError):
    """5xx / transport failure / malformed 200 — retry with backoff."""


class PatRefreshClient:
    def __init__(
        self,
        http_client: httpx.Client,
        *,
        refresh_url: str,
        header_key: str,
        header_value: str,
        timeout: float = 30.0,
    ) -> None:
        self._http = http_client
        self._url = refresh_url
        self._headers = {header_key: header_value}
        self._timeout = timeout

    def refresh(self, current_token: str) -> str:
        """Return the rotated PAT, or raise a typed ``PatRefreshError``."""
        try:
            resp = self._http.put(
                self._url,
                json={"patToken": current_token},
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            logger.warning("pat.refresh_transport_error", error_type=type(exc).__name__)
            raise PatRefreshTransient(str(exc)) from exc

        if resp.status_code == 200:
            return self._extract(resp)
        if resp.status_code == 401:
            raise PatRefreshUnauthorized()
        if resp.status_code == 400:
            raise PatRefreshBadRequest()
        if resp.status_code == 429:
            raise PatRefreshRateLimited()
        logger.warning("pat.refresh_unexpected_status", http_status=resp.status_code)
        raise PatRefreshTransient(f"unexpected status {resp.status_code}")

    @staticmethod
    def _extract(resp: httpx.Response) -> str:
        try:
            body: Any = resp.json()
            token = body["patToken"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PatRefreshTransient("malformed refresh response body") from exc
        if not isinstance(token, str) or not token:
            raise PatRefreshTransient("refresh response carried no patToken")
        return token
