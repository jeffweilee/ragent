"""ADKCaller — proxies a twp-ai run to the upstream ChatAgent service.

Implements the `twp_ai.callers.adk.ADKCaller` protocol (structural). Converts
a `RunAgentInput` into the upstream v2 wire shape (`{metadata, inputData,
stream}`), streams the upstream's SSE response (each line `data: {json}`,
terminal `data: [Done]`), parses each `returnData.messages[]` entry into an
`UpstreamMessage`, and yields those to the caller. Transport / upstream failures
raise typed `UpstreamServiceError` / `UpstreamTimeoutError` so `ADKAgent`
surfaces them as a twp-ai `RUN_ERROR` event with the originating `error_code`.

`user_id` and `user_token` are per-request values (carried in the HTTP
request, not known at startup), so each instance is scoped to one run.
"""

from __future__ import annotations

import json
from collections.abc import Generator

import httpx
import structlog
from twp_ai.callers.adk import UpstreamMessage
from twp_ai.schemas import Message, RunAgentInput

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, classify_upstream_error

logger = structlog.get_logger(__name__)

_UPSTREAM_SUCCESS_CODE = 96200
_HTTPX_ERRORS = (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError)
_SSE_PREFIX = "data: "
_SSE_PREFIX_LEN = len(_SSE_PREFIX)
_SSE_DONE = "[Done]"


class ADKCaller:
    """twp-ai upstream proxy backend for the ChatAgent service."""

    def __init__(
        self,
        *,
        http_client: httpx.Client,
        api_url: str,
        ap_name: str,
        user_id: str,
        user_token: str,
        auth: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = http_client
        self._api_url = api_url
        self._ap_name = ap_name
        self._user_id = user_id
        self._user_token = user_token
        self._headers = {"Authorization": auth} if auth else {}
        self._timeout = timeout

    def stream_deltas(self, request: RunAgentInput, model: str) -> Generator[UpstreamMessage, None, None]:
        payload = {
            "metadata": {
                "apName": self._ap_name,
                "user": self._user_id,
                "userToken": self._user_token,
                "session": request.thread_id,
            },
            "inputData": {"message": _last_user_message(request.messages)},
            "stream": True,
        }

        resp = self._send(payload)
        try:
            yield from _iter_deltas(resp)
        except _HTTPX_ERRORS as exc:
            raise _classify(exc) from exc
        finally:
            resp.close()

    def _send(self, payload: dict) -> httpx.Response:
        resp = None
        try:
            req = self._http.build_request(
                "POST", self._api_url, json=payload, headers=self._headers, timeout=self._timeout
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
        error_code=HttpErrorCode.CHATAGENT_UPSTREAM_ERROR,
        timeout_code=HttpErrorCode.CHATAGENT_TIMEOUT,
    )
    logger.warning("chatagent_v3.upstream_error", http_status=exc_cls.http_status)
    return exc_cls(f"chatagent upstream failed: {exc}", service="chatagent", error_code=error_code)


def _iter_deltas(resp: httpx.Response) -> Generator[UpstreamMessage, None, None]:
    for line in resp.iter_lines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_SSE_PREFIX):
            line = line[_SSE_PREFIX_LEN:]
        if line == _SSE_DONE:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        return_code = obj.get("returnCode")
        if return_code is not None and return_code != _UPSTREAM_SUCCESS_CODE:
            error_msg = obj.get("returnMessage") or "upstream returned non-success code"
            raise UpstreamServiceError(
                error_msg,
                service="chatagent",
                error_code=HttpErrorCode.CHATAGENT_UPSTREAM_ERROR,
            )
        messages = (obj.get("returnData") or {}).get("messages") or []
        for raw in messages:
            yield _parse_message(raw)


def _parse_message(raw: dict) -> UpstreamMessage:
    display_meta = raw.get("displayMeta") or {}
    message_meta = raw.get("messageMeta") or {}
    hitl = raw.get("humanInTheLoopMeta") or {}
    return UpstreamMessage(
        message_id=raw.get("messageId") or "",
        role=raw.get("role", "assistant"),
        content=raw.get("content"),
        agent_type=message_meta.get("langgraph_node"),
        tool_name=display_meta.get("toolName"),
        tool_calls=raw.get("tool_calls") or [],
        finish_reason=raw.get("finish_reason"),
        is_interrupt=bool(hitl.get("isInterrupt")),
        interrupt_message=hitl.get("interruptMessage"),
        interrupt_content=hitl.get("interruptContent"),
    )


def _last_user_message(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content is not None:
            return str(message.content)
    return ""
