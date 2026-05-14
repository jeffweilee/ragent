"""T-HTTPLOG — diagnostic logging for upstream HTTP failures.

`install_error_logging(client, *, client_name, redact_auth_body=False)`
wraps `httpx.Client.send` so every non-2xx response and every transport
exception (`httpx.HTTPError`: timeout, connect, read, …) emits a single
structured `http.upstream_error` record carrying the request body, response
body (when available), redacted headers, status, and exception type.

Body emission uses keys `http_request_payload` / `http_response_payload`
which sit outside the project's logging denylist
(`bootstrap/logging_config.py::_DENY_KEYS`) — a deliberate carve-out for
upstream-error diagnostics. `Authorization`, `apikey`, and `Cookie`
headers are redacted at source. When `redact_auth_body=True` the JSON
`key` field of the request body (the J1 token sent to `AI_API_AUTH_URL`)
is replaced with ``"***"`` before logging.

Implementation note: httpx `event_hooks` do NOT fire on transport
exceptions, so a `send` wrapper is used instead. If we later migrate to
`httpx.AsyncClient` or to a custom `httpx.BaseTransport`, the same logic
should be re-expressed at the transport layer.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import structlog

_logger = structlog.get_logger(__name__)

_REDACT_HEADERS = frozenset({"authorization", "apikey", "cookie"})
_DEFAULT_MAX_BYTES = 8192


def _max_bytes() -> int:
    raw = os.environ.get("HTTP_ERROR_LOG_MAX_BYTES")
    if raw is None:
        return _DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _decode_and_truncate(payload: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(payload) > max_bytes
    clipped = payload[:max_bytes] if truncated else payload
    return clipped.decode("utf-8", errors="replace"), truncated


def _redact_headers(headers: httpx.Headers) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for k, v in headers.items():
        redacted[k] = "***" if k.lower() in _REDACT_HEADERS else v
    return redacted


def _redact_auth_body(body: bytes) -> bytes:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if isinstance(parsed, dict) and "key" in parsed:
        parsed["key"] = "***"
        return json.dumps(parsed).encode("utf-8")
    return body


def _emit(
    *,
    client_name: str,
    request: httpx.Request,
    response: httpx.Response | None,
    exception: BaseException | None,
    is_stream: bool,
    redact_auth_body: bool,
) -> None:
    max_bytes = _max_bytes()
    request_body = _redact_auth_body(request.content) if redact_auth_body else request.content
    req_text, req_trunc = _decode_and_truncate(request_body, max_bytes)
    fields: dict[str, Any] = {
        "client_name": client_name,
        "method": request.method,
        "url": str(request.url),
        "headers": _redact_headers(request.headers),
        "http_request_payload": req_text,
        "request_truncated": req_trunc,
        "status": response.status_code if response is not None else None,
        "exception_type": type(exception).__name__ if exception is not None else None,
    }
    if response is not None and not is_stream:
        try:
            resp_bytes = response.content
        except (httpx.StreamConsumed, httpx.ResponseNotRead, httpx.StreamClosed):
            resp_bytes = None
        if resp_bytes is not None:
            resp_text, resp_trunc = _decode_and_truncate(resp_bytes, max_bytes)
            fields["http_response_payload"] = resp_text
            fields["response_truncated"] = resp_trunc
    _logger.error("http.upstream_error", **fields)


def install_error_logging(
    client: httpx.Client,
    *,
    client_name: str,
    redact_auth_body: bool = False,
) -> None:
    """Wrap `client.send` so HTTP errors emit `http.upstream_error` records."""
    original_send = client.send

    def wrapped_send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            response = original_send(request, **kwargs)
        except httpx.HTTPError as exc:
            _emit(
                client_name=client_name,
                request=request,
                response=None,
                exception=exc,
                is_stream=bool(kwargs.get("stream", False)),
                redact_auth_body=redact_auth_body,
            )
            raise
        if response.status_code >= 400:
            _emit(
                client_name=client_name,
                request=request,
                response=response,
                exception=None,
                is_stream=bool(kwargs.get("stream", False)),
                redact_auth_body=redact_auth_body,
            )
        return response

    client.send = wrapped_send  # type: ignore[method-assign]
    client.__ragent_http_error_logging__ = True  # type: ignore[attr-defined]
