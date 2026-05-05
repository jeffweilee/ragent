"""T3.7 — LLMClient.chat: non-streaming, usage, retry 3×@2s, timeout (B28)."""

from unittest.mock import MagicMock

import pytest

from ragent.clients.llm import LLMClient


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


def test_chat_uses_bearer_token():
    http = _mock_http("ok")
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "secret")
    client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer secret"


def test_chat_uses_timeout():
    http = _mock_http("ok")
    client = LLMClient(
        api_url="https://llm.example.com", http=http, get_token=lambda: "tok", timeout=77
    )
    client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    timeout = http.post.call_args[1]["timeout"]
    assert timeout == 77


def test_chat_retries_3_times_on_error():
    sleep_calls: list[float] = []
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
        sleep=lambda s: sleep_calls.append(s),
    )
    result = client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert result["content"] == "ok"
    assert http.post.call_count == 3
    assert sleep_calls == [2.0, 2.0]


def test_chat_raises_after_3_failures():
    http = MagicMock()
    http.post.side_effect = Exception("fail")
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(Exception, match="fail"):  # noqa: B017
        client.chat(messages=[{"role": "user", "content": "q"}], model="m")
    assert http.post.call_count == 3
