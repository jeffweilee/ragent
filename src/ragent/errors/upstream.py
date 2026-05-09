"""Typed exceptions for upstream-service failures.

`UpstreamServiceError` (HTTP 502) and `UpstreamTimeoutError` (HTTP 504)
let the global FastAPI handler distinguish "the third party broke" from
"we crashed" — required by `00_rule.md` §API Error Honesty.

Client retries (`embedding`, `llm`, `rerank`) wrap the last exception in
one of these types after exhaustion; the global handler reads
`error_code` + `http_status` via `getattr` and surfaces both in the
RFC 9457 problem-details response and in the failure log line.
"""

from __future__ import annotations


class UpstreamServiceError(Exception):
    """Generic upstream-service failure (5xx, malformed response, retry exhaustion)."""

    http_status: int = 502
    error_code: str = "UPSTREAM_ERROR"

    def __init__(
        self,
        message: str,
        *,
        service: str,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        if error_code is not None:
            self.error_code = error_code


class UpstreamTimeoutError(UpstreamServiceError):
    """Upstream did not respond within the configured deadline."""

    http_status: int = 504
    error_code: str = "UPSTREAM_TIMEOUT"
