"""T3.7 — LLMClient.chat: non-streaming, usage, retry 3×@2s, timeout (B28)."""

from unittest.mock import MagicMock

import pytest

from ragent.clients.llm import LLMClient
from ragent.errors.upstream import UpstreamServiceError


def _mock_http(content: str, usage: dict | None = None) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    http.post.return_value = resp
    return http


def test_chat_returns_content_and_usage():
    http = _mock_http("Hello!", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "tok")
    result = client.chat(messages=[{"role": "user", "content": "hi"}], model="gptoss-120b")
    assert result["content"] == "Hello!"
    assert result["usage"]["promptTokens"] == 10
    assert result["usage"]["completionTokens"] == 5
    assert result["usage"]["totalTokens"] == 15


def test_chat_post_shape():
    http = _mock_http("ok")
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "tok")
    client.chat(
        messages=[{"role": "user", "content": "q"}],
        model="gptoss-120b",
        temperature=0.5,
        max_tokens=100,
    )
    body = http.post.call_args[1]["json"]
    assert body["model"] == "gptoss-120b"
    assert body["messages"] == [{"role": "user", "content": "q"}]
    assert body["stream"] is False
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 100


def test_chat_uses_raw_token_no_bearer():
    http = _mock_http("ok")
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "secret")
    client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "secret"


def test_chat_uses_timeout():
    http = _mock_http("ok")
    client = LLMClient(
        api_url="https://llm.example.com", http=http, get_token=lambda: "tok", timeout=77
    )
    client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    timeout = http.post.call_args[1]["timeout"]
    assert timeout == 77


def test_chat_honours_explicit_zero_timeout(monkeypatch):
    """T-APL.3 — `timeout=0` must reach httpx (not be swallowed by `value or env`).

    The constructor previously read `self._timeout = timeout or float(env)`,
    so an explicit `0` (operator-meaningful: "0 seconds" in httpx semantics,
    or a fail-fast signal) collapsed to the env default. Pinned here so the
    falsy-check pattern cannot regress.
    """
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    http = _mock_http("ok")
    client = LLMClient(
        api_url="https://llm.example.com", http=http, get_token=lambda: "tok", timeout=0
    )
    client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert http.post.call_args[1]["timeout"] == 0


def test_chat_does_not_retry_a_transient_error():
    """T-RETRY.1 — one prompt in, one upstream call out."""
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise Exception("connection error")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }
        return resp

    http = MagicMock()
    http.post.side_effect = side_effect
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError):
        client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert http.post.call_count == 1


def test_chat_raises_upstream_service_error_on_first_failure():
    http = MagicMock()
    http.post.side_effect = Exception("boom")
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert exc_info.value.error_code == "LLM_ERROR"
    assert exc_info.value.http_status == 502
    # The raw exception text must not leak into the client-visible message —
    # it's still available server-side via __cause__ for logging/debugging.
    assert "boom" not in str(exc_info.value)
    assert "boom" in str(exc_info.value.__cause__)
    assert http.post.call_count == 1


def test_chat_raises_when_content_is_none() -> None:
    """Null/empty LLM content must raise — silent None reaching answer is hallucination-prone.

    The underlying ``ValueError`` is wrapped in ``UpstreamServiceError(LLM_ERROR)``
    per `00_rule.md` §API Error Honesty. **Not retried** (operator decision
    2026-08-10, T-RETRY.1): re-prompting on an empty completion doubles the
    generation cost and latency for a request the caller may already have
    abandoned, and the caller can always ask again.
    """
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": None}}],
        "usage": {},
    }
    http.post.return_value = resp
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "empty" in str(exc_info.value.__cause__)
    assert http.post.call_count == 1


def test_chat_survives_explicit_null_usage() -> None:
    """`{"usage": null}` must not crash. `data.get("usage", {})` returns None for
    an explicit JSON null, and T-RETRY.1 moved the `usage_raw.get(...)` reads
    outside the try — so what used to surface as a wrapped 502 would now be an
    unhandled AttributeError (500). PR #247 review, pr-agent."""
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": None,
    }
    http.post.return_value = resp
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
    )

    result = client.chat(messages=[{"role": "user", "content": "q"}], model="m")

    assert result["content"] == "ok"
    assert result["usage"] == {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}


def test_chat_raises_when_content_is_empty_string() -> None:
    http = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": "   "}}],
        "usage": {},
    }
    http.post.return_value = resp
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "empty" in str(exc_info.value.__cause__)
    assert http.post.call_count == 1
