"""T4.6 — LLMClient: sync streaming via SSE, LLM_TIMEOUT_SECONDS (B28).

**No application-level retry** (T-RETRY.1). At a 120 s timeout the old 3×@2s
loop held a caller for up to ~364 s before surfacing the failure, and each
attempt re-issued the prompt — a second billed generation for one request. An
empty completion is likewise terminal now (operator decision 2026-08-10): the
caller can ask again, and re-prompting behind their back doubles cost and
latency for a request they may already have abandoned. Connection establishment
is retried by the shared httpx transport (`bootstrap/composition.py`).
"""

import json
import os
from collections.abc import Callable, Generator
from typing import Any

import structlog
from opentelemetry import trace

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import classify_upstream_error
from ragent.utility.env import float_env_or

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)
# Client-visible message for an upstream failure. Never interpolate the raw
# upstream exception text here — it may carry httpx URL/host/port detail; the
# real exception goes to the server log via `error_type` only.
_LLM_GENERIC_MESSAGE = "llm upstream request failed"


class LLMClient:
    def __init__(
        self,
        api_url: str,
        http: Any,
        get_token: Callable[[], str],
        timeout: float | None = None,
        auth_header_name: str | None = None,
    ) -> None:
        self._url = api_url
        self._http = http
        self._get_token = get_token
        self._timeout = float_env_or(timeout, "LLM_TIMEOUT_SECONDS", 120.0)
        self._auth_header_name = auth_header_name or os.environ.get(
            "LLM_AUTH_HEADER_NAME", "Authorization"
        )

    def stream(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        usage_out: list | None = None,
    ) -> Generator[str, None, None]:
        from ragent.errors.upstream import LLMStreamInterruptedError

        with _tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("peer.service", "llm")
            span.set_attribute("model", model)
            try:
                yield from self._do_stream(
                    messages, model, temperature, max_tokens, usage_out=usage_out
                )
            except LLMStreamInterruptedError:
                # Partial content already yielded downstream — the caller must
                # see the interruption, not a re-issued stream.
                raise
            except Exception as exc:
                span.record_exception(exc)
                error_code, exc_cls = classify_upstream_error(
                    exc,
                    error_code=HttpErrorCode.LLM_ERROR,
                    timeout_code=HttpErrorCode.LLM_TIMEOUT,
                )
                logger.error(
                    "llm.error",
                    peer_service="llm",
                    model=model,
                    error_type=type(exc).__name__,
                    error_code=error_code,
                )
                raise exc_cls(
                    _LLM_GENERIC_MESSAGE,
                    service="llm",
                    error_code=error_code,
                ) from exc
            logger.info("llm.call", peer_service="llm", model=model)

    def _do_stream(
        self, messages, model, temperature, max_tokens, usage_out: list | None = None
    ) -> Generator[str, None, None]:
        from ragent.errors.upstream import LLMStreamInterruptedError

        seen_done = False
        any_content = False
        resp = self._http.post(
            self._url,
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            },
            headers={self._auth_header_name: self._get_token()},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if data_str == "[DONE]":
                seen_done = True
                break
            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices", [])
            if choices:
                content = choices[0].get("delta", {}).get("content")
                if content:
                    any_content = True
                    yield content
            if usage_out is not None and "usage" in payload:
                usage_out.append(payload["usage"])
        if not seen_done and any_content:
            raise LLMStreamInterruptedError("LLM stream closed before [DONE]")

    def stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Generator[tuple[str, Any], None, None]:
        """Stream with optional tool calls; same failure contract as stream().

        Yields:
            ("text",      delta: str)
            ("tool_call", {"id": str, "name": str, "arguments": str})
        """
        from ragent.errors.upstream import LLMStreamInterruptedError

        with _tracer.start_as_current_span("llm.stream_with_tools") as span:
            span.set_attribute("peer.service", "llm")
            span.set_attribute("model", model)
            try:
                yield from self._do_stream_with_tools(
                    messages, tools, model, temperature, max_tokens
                )
            except LLMStreamInterruptedError:
                raise
            except Exception as exc:
                span.record_exception(exc)
                error_code, exc_cls = classify_upstream_error(
                    exc,
                    error_code=HttpErrorCode.LLM_ERROR,
                    timeout_code=HttpErrorCode.LLM_TIMEOUT,
                )
                logger.error(
                    "llm.error",
                    peer_service="llm",
                    model=model,
                    error_type=type(exc).__name__,
                    error_code=error_code,
                )
                raise exc_cls(
                    _LLM_GENERIC_MESSAGE,
                    service="llm",
                    error_code=error_code,
                ) from exc
            logger.info("llm.call_with_tools", peer_service="llm", model=model)

    def _do_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> Generator[tuple[str, Any], None, None]:
        from ragent.errors.upstream import LLMStreamInterruptedError

        seen_done = False
        any_content = False
        tool_calls_acc: dict[int, dict] = {}

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        resp = self._http.post(
            self._url,
            json=body,
            headers={self._auth_header_name: self._get_token()},
            timeout=self._timeout,
        )
        resp.raise_for_status()

        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if data_str == "[DONE]":
                seen_done = True
                break
            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = payload.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}

            content = delta.get("content")
            if content:
                any_content = True
                yield ("text", content)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.get("id"):
                    tool_calls_acc[idx]["id"] = tc["id"]
                func = tc.get("function") or {}
                if func.get("name"):
                    tool_calls_acc[idx]["name"] += func["name"]
                if func.get("arguments"):
                    tool_calls_acc[idx]["arguments"] += func["arguments"]

        if not seen_done and any_content:
            raise LLMStreamInterruptedError("LLM stream closed before [DONE]")

        for _, tc_data in sorted(tool_calls_acc.items()):
            yield ("tool_call", tc_data)

    def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict:
        """Non-streaming chat — collects full response with usage."""
        with _tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute("peer.service", "llm")
            span.set_attribute("model", model)
            try:
                resp = self._http.post(
                    self._url,
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    headers={self._auth_header_name: self._get_token()},
                    timeout=self._timeout,
                )
                span.set_attribute("http.status_code", getattr(resp, "status_code", 0))
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content")
                if not isinstance(content, str) or not content.strip():
                    # Terminal, not retryable: forwarding `None` as the answer is
                    # hallucination-prone, and re-prompting behind the caller's
                    # back bills a second generation for one request (T-RETRY.1).
                    raise ValueError("LLM returned empty content")
                # `or {}` not `get("usage", {})` — an explicit JSON `null` is a
                # real upstream shape, and the reads below sit outside the try,
                # where an AttributeError would escape as a 500 instead of the
                # typed 502.
                usage_raw = data.get("usage") or {}
            except Exception as exc:
                span.record_exception(exc)
                error_code, exc_cls = classify_upstream_error(
                    exc,
                    error_code=HttpErrorCode.LLM_ERROR,
                    timeout_code=HttpErrorCode.LLM_TIMEOUT,
                )
                logger.error(
                    "llm.error",
                    peer_service="llm",
                    model=model,
                    error_type=type(exc).__name__,
                    error_code=error_code,
                )
                raise exc_cls(
                    _LLM_GENERIC_MESSAGE,
                    service="llm",
                    error_code=error_code,
                ) from exc
            span.set_attribute("prompt_tokens", int(usage_raw.get("prompt_tokens", 0)))
            span.set_attribute("completion_tokens", int(usage_raw.get("completion_tokens", 0)))
            logger.info(
                "llm.call",
                peer_service="llm",
                model=model,
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
                content_length=len(content),
            )
            return {
                "content": content,
                "usage": {
                    "promptTokens": usage_raw.get("prompt_tokens", 0),
                    "completionTokens": usage_raw.get("completion_tokens", 0),
                    "totalTokens": usage_raw.get("total_tokens", 0),
                },
            }
