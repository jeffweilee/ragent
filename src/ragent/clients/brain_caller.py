"""BrainCaller — relays a twp-ai run to the ragent-brain upstream.

Implements the `twp_ai.callers.brain.BrainCaller` protocol (structural). Unlike
`ADKCaller`, brain speaks twp-ai natively: it accepts the `RunAgentInput` body
as-is on `POST {brain_url}/run` and streams back the twp-ai SSE lifecycle
itself. So this caller does **no** wire translation — it forwards the body
verbatim and yields brain's SSE frames unchanged (`data: {json}\n\n`) for
`BrainAgent` to relay. Transport / upstream failures raise typed
`UpstreamServiceError` / `UpstreamTimeoutError` so `BrainAgent` surfaces them as
a twp-ai `RUN_ERROR` event with the originating `error_code`.

`user_id` is a per-request value (carried as the `X-User-Id` header brain scopes
data by), so each instance is scoped to one run — mirroring `ADKCaller`.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx
import structlog
from twp_ai.schemas import RunAgentInput

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, classify_upstream_error

logger = structlog.get_logger(__name__)

_HTTPX_ERRORS = (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError)
_SSE_PREFIX = "data: "
# Client-visible message for any upstream failure. Never interpolate upstream or
# httpx exception text here — the raw detail goes to the server log only.
_UPSTREAM_GENERIC_MESSAGE = "brain upstream request failed"


class BrainCaller:
    """twp-ai-native relay backend for the ragent-brain service."""

    def __init__(
        self,
        *,
        http_client: httpx.Client,
        brain_url: str,
        user_id: str,
        brain_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = http_client
        self._run_url = f"{brain_url.rstrip('/')}/run"
        self._headers = {"X-User-Id": user_id}
        if brain_key:
            self._headers["X-Brain-Key"] = brain_key
        self._timeout = timeout

    def stream_frames(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
        # Forward the body verbatim (camelCase wire form). brain reads the
        # structured context/state fields directly, so no <hidden> preamble is
        # built. Do NOT drop null fields — `state`/`forwardedProps` are required
        # (nullable) in RunAgentInput, so excluding their null values would make a
        # twp-ai-native brain reject the run with 422. `model` is only injected
        # when the body did not already carry one.
        payload = request.model_dump(by_alias=True)
        if model and not payload.get("model"):
            payload["model"] = model

        resp = self._send(payload)
        try:
            for line in resp.iter_lines():
                line = line.rstrip("\r")
                if line.startswith(_SSE_PREFIX):
                    # Reconstruct the SSE frame (iter_lines strips the newlines);
                    # brain's to_sse emits single-line `data: {json}` blocks.
                    yield f"{line}\n\n"
        except _HTTPX_ERRORS as exc:
            raise _classify(exc) from exc
        finally:
            resp.close()

    def _send(self, payload: dict) -> httpx.Response:
        resp = None
        try:
            req = self._http.build_request(
                "POST", self._run_url, json=payload, headers=self._headers, timeout=self._timeout
            )
            resp = self._http.send(req, stream=True)
            resp.raise_for_status()
            return resp
        except _HTTPX_ERRORS as exc:
            if resp is not None:
                resp.close()
            raise _classify(exc) from exc


def _classify(exc: httpx.HTTPError) -> UpstreamServiceError:
    error_code, exc_cls = classify_upstream_error(
        exc,
        error_code=HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR,
        timeout_code=HttpErrorCode.BRAINAGENT_TIMEOUT,
    )
    logger.warning(
        "brainagent.upstream_error",
        http_status=exc_cls.http_status,
        error_type=type(exc).__name__,
    )
    return exc_cls(_UPSTREAM_GENERIC_MESSAGE, service="brain", error_code=error_code)
