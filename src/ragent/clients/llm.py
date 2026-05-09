"""T4.6 — LLMClient: sync streaming via SSE, retry 3×@2s, LLM_TIMEOUT_SECONDS (B28)."""

import json
import os
import time as _time
from collections.abc import Callable, Generator
from typing import Any

import structlog
from opentelemetry import trace

from ragent.errors.upstream import classify_upstream_error

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class LLMClient:
    def __init__(
        self,
        api_url: str,
        http: Any,
        get_token: Callable[[], str],
        timeout: float | None = None,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self._url = api_url.rstrip("/") + "/gpt_oss_120b/v1/chat/completions"
        self._http = http
        self._get_token = get_token
        self._timeout = timeout or float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))
        self._sleep = sleep

    def stream(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Generator[str, None, None]:
        with _tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("peer.service", "llm")
            span.set_attribute("model", model)
            last_exc: Exception | None = None
            for attempt in range(3):
                if attempt:
                    self._sleep(2.0)
                try:
                    span.set_attribute("retry_attempt", attempt)
                    yield from self._do_stream(messages, model, temperature, max_tokens)
                    logger.info("llm.call", peer_service="llm", model=model, retry_attempt=attempt)
                    return
                except Exception as exc:
                    last_exc = exc
            span.record_exception(last_exc)  # type: ignore[arg-type]
            error_code, exc_cls = classify_upstream_error(
                last_exc, error_code="LLM_ERROR", timeout_code="LLM_TIMEOUT"
            )
            logger.error(
                "llm.error",
                peer_service="llm",
                model=model,
                error_type=type(last_exc).__name__ if last_exc else None,
                error_code=error_code,
            )
            raise exc_cls(
                f"llm stream failed after retries: {last_exc}",
                service="llm",
                error_code=error_code,
            ) from last_exc

    def _do_stream(self, messages, model, temperature, max_tokens) -> Generator[str, None, None]:
        with self._http.post(
            self._url,
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            },
            headers={"Authorization": f"Bearer {self._get_token()}"},
            timeout=self._timeout,
        ) as resp:
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    payload = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = payload.get("choices", [])
                if choices:
                    content = choices[0].get("delta", {}).get("content")
                    if content:
                        yield content

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
            last_exc: Exception | None = None
            for attempt in range(3):
                if attempt:
                    self._sleep(2.0)
                try:
                    span.set_attribute("retry_attempt", attempt)
                    resp = self._http.post(
                        self._url,
                        json={
                            "model": model,
                            "messages": messages,
                            "stream": False,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                        },
                        headers={"Authorization": f"Bearer {self._get_token()}"},
                        timeout=self._timeout,
                    )
                    span.set_attribute("http.status_code", getattr(resp, "status_code", 0))
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"].get("content")
                    if not isinstance(content, str) or not content.strip():
                        # Treat null / whitespace-only completions as a retryable
                        # failure rather than forwarding `None` as the answer.
                        raise ValueError("LLM returned empty content")
                    usage_raw = data.get("usage", {})
                    span.set_attribute("prompt_tokens", int(usage_raw.get("prompt_tokens", 0)))
                    span.set_attribute(
                        "completion_tokens", int(usage_raw.get("completion_tokens", 0))
                    )
                    logger.info(
                        "llm.call",
                        peer_service="llm",
                        model=model,
                        retry_attempt=attempt,
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
                except Exception as exc:
                    last_exc = exc
            span.record_exception(last_exc)  # type: ignore[arg-type]
            error_code, exc_cls = classify_upstream_error(
                last_exc, error_code="LLM_ERROR", timeout_code="LLM_TIMEOUT"
            )
            logger.error(
                "llm.error",
                peer_service="llm",
                model=model,
                error_type=type(last_exc).__name__ if last_exc else None,
                error_code=error_code,
            )
            raise exc_cls(
                f"llm chat failed after retries: {last_exc}",
                service="llm",
                error_code=error_code,
            ) from last_exc
