"""Per-request structured logging middleware.

Emits exactly one ``api.request`` (or ``api.error``) record per HTTP request
with a stable ``request_id``, identity-only fields (no body, no query string),
and OTEL trace correlation via ``structlog.contextvars``. Re-raises exceptions
so the application's RFC 9457 problem handler still produces the response.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)

_SKIP_PATHS = frozenset({"/livez", "/readyz", "/metrics"})
_REQUEST_ID_HEADER = "X-Request-Id"
_USER_ID_HEADER = "X-User-Id"
_MAX_REQUEST_ID_LEN = 128
_VALID_REQUEST_ID = (  # printable ASCII without whitespace / control chars
    set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
)


def _coerce_request_id(raw: str | None) -> str:
    if not raw:
        return str(uuid.uuid4())
    if len(raw) > _MAX_REQUEST_ID_LEN:
        return str(uuid.uuid4())
    if any(c not in _VALID_REQUEST_ID for c in raw):
        return str(uuid.uuid4())
    return raw


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log each request with method, path, status, duration, identity ids only."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        request_id = _coerce_request_id(request.headers.get(_REQUEST_ID_HEADER))
        user_id = request.headers.get(_USER_ID_HEADER)

        identity: dict[str, Any] = {"request_id": request_id}
        if user_id:
            identity["user_id"] = user_id
        structlog.contextvars.bind_contextvars(**identity)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "api.error",
                method=request.method,
                path=path,
                duration_ms=round((time.perf_counter() - start) * 1000.0, 3),
                **identity,
            )
            structlog.contextvars.unbind_contextvars("request_id", "user_id")
            raise
        logger.info(
            "api.request",
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000.0, 3),
            **identity,
        )
        response.headers[_REQUEST_ID_HEADER] = request_id
        structlog.contextvars.unbind_contextvars("request_id", "user_id")
        return response
