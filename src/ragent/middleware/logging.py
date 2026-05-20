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

_SKIP_PATHS = frozenset({"/livez", "/readyz", "/startupz", "/metrics"})
_REQUEST_ID_HEADER = "X-Request-Id"
_USER_ID_HEADER = "X-User-Id"
# ASGI scope key written by ``bootstrap/app.py::_x_user_id_middleware`` after
# JWT verification (or trust-header acceptance). Read here AFTER ``call_next``
# to surface the resolved user_id on ``api.request`` / ``api.error`` logs.
# A dict-key channel is used instead of the X-User-Id header because Starlette
# constructs a fresh ``Headers(scope=...)`` view per ``BaseHTTPMiddleware``
# boundary — each view captures a different list reference, so header mutations
# inside the inner middleware don't propagate back to the outer middleware's
# cached headers view. The scope dict itself IS shared, so a plain key works.
SCOPE_USER_ID_KEY = "ragent.user_id"
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
        # In trust-header mode the X-User-Id arrives on the inbound request;
        # in JWT mode the inner `_x_user_id_middleware` injects it AFTER we
        # run, via ASGI scope mutation. Snapshot here for handler-time
        # contextvar correlation (trust-header path), then re-read after
        # call_next so the final api.request/api.error log carries the
        # JWT-mode injected value too.
        inbound_user_id = request.headers.get(_USER_ID_HEADER)

        identity: dict[str, Any] = {"request_id": request_id}
        if inbound_user_id:
            identity["user_id"] = inbound_user_id
        structlog.contextvars.bind_contextvars(**identity)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                final = _final_identity(request, request_id, inbound_user_id)
                logger.exception(
                    "api.error",
                    method=request.method,
                    path=path,
                    duration_ms=round((time.perf_counter() - start) * 1000.0, 3),
                    **final,
                )
                raise
            final = _final_identity(request, request_id, inbound_user_id)
            logger.info(
                "api.request",
                method=request.method,
                path=path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - start) * 1000.0, 3),
                **final,
            )
            response.headers[_REQUEST_ID_HEADER] = request_id
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "user_id")


def _final_identity(
    request: Request, request_id: str, inbound_user_id: str | None
) -> dict[str, Any]:
    """Resolve the per-request identity for the final api.request/api.error log.

    JWT mode: the inner auth middleware writes the resolved user_id into
    ``request.scope[SCOPE_USER_ID_KEY]`` (a plain dict-key channel that
    survives Starlette's per-Request Headers replacement).
    Trust-header / public paths: no scope key is set; fall back to whatever
    arrived on the inbound ``X-User-Id`` header.
    """
    scope_user_id = request.scope.get(SCOPE_USER_ID_KEY)
    user_id = scope_user_id or inbound_user_id
    identity: dict[str, Any] = {"request_id": request_id}
    if user_id:
        identity["user_id"] = user_id
    return identity
