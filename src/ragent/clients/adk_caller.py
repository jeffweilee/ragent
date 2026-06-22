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
import re
from collections.abc import Generator

import httpx
import structlog
from twp_ai.callers.adk import UpstreamMessage
from twp_ai.schemas import ContextItem, Message, RunAgentInput

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, classify_upstream_error

logger = structlog.get_logger(__name__)

_UPSTREAM_SUCCESS_CODE = 96200
_HTTPX_ERRORS = (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError)
_SSE_PREFIX = "data: "
_SSE_PREFIX_LEN = len(_SSE_PREFIX)
_SSE_DONE = "[Done]"


class ResumeValidationError(Exception):
    """A /chatagent/v3 resume payload the upstream cannot honour.

    Carries `error_code` so ADKAgent surfaces it as a RUN_ERROR (v3 maps every
    failure onto a 200 stream, never an HTTP 4xx).
    """

    error_code = HttpErrorCode.CHATAGENT_INVALID_RESUME


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

    def stream_deltas(
        self, request: RunAgentInput, model: str
    ) -> Generator[UpstreamMessage, None, None]:
        input_data = _resume_input_data(request)
        if input_data is None:
            # All interrupts were cancelled — no upstream call; the run finishes
            # successfully with an empty body.
            return
        payload = {
            "metadata": {
                "apName": self._ap_name,
                "user": self._user_id,
                "userToken": self._user_token,
                "session": request.thread_id,
            },
            "inputData": input_data,
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
    seen_done = False
    for line in resp.iter_lines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_SSE_PREFIX):
            line = line[_SSE_PREFIX_LEN:]
        if line == _SSE_DONE:
            seen_done = True
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        return_code = obj.get("returnCode")
        if return_code is not None and return_code != _UPSTREAM_SUCCESS_CODE:
            error_msg = obj.get("returnMessage") or "upstream returned non-success code"
            raise UpstreamServiceError(
                error_msg,
                service="chatagent",
                error_code=HttpErrorCode.CHATAGENT_UPSTREAM_ERROR,
            )
        return_data = obj.get("returnData")
        messages = return_data.get("messages") or [] if isinstance(return_data, dict) else []
        for raw in messages:
            if isinstance(raw, dict):
                yield _parse_message(raw)
    if not seen_done:
        raise UpstreamServiceError(
            "upstream closed stream without [Done] sentinel",
            service="chatagent",
            error_code=HttpErrorCode.CHATAGENT_UPSTREAM_ERROR,
        )


def _parse_message(raw: dict) -> UpstreamMessage:
    display_meta = raw.get("displayMeta")
    if not isinstance(display_meta, dict):
        display_meta = {}
    message_meta = raw.get("messageMeta")
    if not isinstance(message_meta, dict):
        message_meta = {}
    hitl = raw.get("humanInTheLoopMeta")
    if not isinstance(hitl, dict):
        hitl = {}
    return UpstreamMessage(
        message_id=raw.get("messageId") or "",
        role=raw.get("role") or "assistant",
        # No <hidden> strip here: the stream carries the agent's own generated
        # deltas (assistant/reasoning/tool), never the user turn that carries the
        # preamble — stripping belongs only to the session-history read path.
        content=raw.get("content"),
        agent_type=message_meta.get("langgraph_node"),
        tool_name=display_meta.get("toolName"),
        display_meta=display_meta or None,
        tool_calls=raw.get("tool_calls") or [],
        finish_reason=raw.get("finish_reason"),
        is_interrupt=bool(hitl.get("isInterrupt")),
        interrupt_message=hitl.get("interruptMessage"),
        interrupt_content=hitl.get("interruptContent"),
    )


def _resume_input_data(request: RunAgentInput) -> dict | None:
    """Build the upstream `inputData` for a turn.

    A normal turn sends `{message}`. A resume turn answers a prior interrupt:
    `resolved` continues the upstream run via `{lastMessageId, message: ""}`
    (the upstream only supports go / no-go, so `payload` is dropped); a turn
    whose interrupts are all `cancelled` returns ``None`` so no upstream call is
    made. More than one `resolved` interrupt is rejected — the upstream takes a
    single `lastMessageId`.
    """
    resume = request.resume
    if not resume:
        return {"message": _compose_message(request)}
    resolved = [item for item in resume if item.status == "resolved"]
    if len(resolved) > 1:
        raise ResumeValidationError("resume accepts at most one resolved interrupt per turn")
    if not resolved:
        return None
    return {"lastMessageId": resolved[0].interrupt_id, "message": ""}


def _compose_message(request: RunAgentInput) -> str:
    """Prepend the client-supplied context/state ahead of the user's question.

    The upstream is a general, tool-capable agent that owns its own persona and
    keeps conversation memory by `session = threadId`, so we deliberately do NOT
    impose an assistant persona or enumerate tools — we only surface the
    `context`/`state` the single `inputData.message` field would otherwise drop.
    With nothing to inject the turn stays a plain pass-through.

    The context/state are wrapped in a `<hidden>…</hidden>` block (with nested
    `<context>`/`<state>` tags). The frontend strips that block before rendering
    so it never leaks into the visible agent history, while the upstream agent —
    whose system prompt is told the block carries machine-supplied context — can
    still read it.
    """
    user_message = _last_user_message(request.messages)
    preamble = _context_preamble(request.context, request.state)
    if not preamble:
        return user_message
    if not user_message:
        return preamble
    return f"{preamble}\n\n{user_message}"


# json.dumps does not escape `<`/`>`, so a payload value containing a literal
# wrapper tag would close the <hidden> block early and break the frontend strip
# (the same hazard schemas/chat.py and routers/mcp.py neutralize for <context>).
# A lenient stripper also honours whitespace/attributes (`</hidden >`,
# `<hidden attr="1">`), so those forms are neutralized too — anything a relaxed
# HTML/XML parser would accept as one of our wrapper tags.
_WRAPPER_TAG_RE = re.compile(r"<(/?\s*(?:hidden|context|state)(?:\s+[^>]*)?)>", re.IGNORECASE)


def _neutralize_wrapper_tags(value: str) -> str:
    return _WRAPPER_TAG_RE.sub(r"&lt;\1&gt;", value)


def _context_preamble(context: list[ContextItem], state: object) -> str:
    sections: list[str] = []
    if context:
        context_json = json.dumps(
            [item.model_dump(by_alias=True) for item in context],
            ensure_ascii=False,
        )
        sections.append(f"<context>{_neutralize_wrapper_tags(context_json)}</context>")
    if state is not None:
        state_json = json.dumps(state, ensure_ascii=False)
        sections.append(f"<state>{_neutralize_wrapper_tags(state_json)}</state>")
    if not sections:
        return ""
    return "<hidden>\n" + "\n".join(sections) + "\n</hidden>"


def _last_user_message(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content is not None:
            return str(message.content)
    return ""
