"""T4.5 — LLMClient: streaming iterator, timeout, retry 3×@2s."""

import json
from unittest.mock import MagicMock

import httpx
import pytest

from ragent.clients.llm import LLMClient
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError


def _sse_lines(deltas: list[str], usage: dict | None = None) -> list[bytes]:
    lines = []
    for d in deltas:
        payload = {"choices": [{"delta": {"content": d}, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(payload)}\n\n".encode())
    # done event with usage
    done_payload: dict = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    if usage:
        done_payload["usage"] = usage
    lines.append(f"data: {json.dumps(done_payload)}\n\n".encode())
    lines.append(b"data: [DONE]\n\n")
    return lines


def _mock_streaming_http(deltas, usage=None):
    http = MagicMock()
    lines = _sse_lines(deltas, usage)

    class _FakeStream:
        def __init__(self):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_lines(self):
            for line in self._lines:
                yield line.decode().strip()

    http.post.return_value = _FakeStream()
    return http


def test_stream_yields_deltas():
    http = _mock_streaming_http(["Hello", " world"])
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "tok")
    deltas = list(client.stream(messages=[{"role": "user", "content": "hi"}], model="gptoss-120b"))
    assert deltas == ["Hello", " world"]


def test_stream_post_shape():
    http = _mock_streaming_http(["ok"])
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "tok")
    list(
        client.stream(
            messages=[{"role": "user", "content": "q"}],
            model="gptoss-120b",
            temperature=0.5,
            max_tokens=100,
        )
    )
    body = http.post.call_args[1]["json"]
    assert body["model"] == "gptoss-120b"
    assert body["messages"] == [{"role": "user", "content": "q"}]
    assert body["stream"] is True
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 100
    assert body.get("stream_options", {}).get("include_usage") is True


def test_stream_bearer_token():
    http = _mock_streaming_http(["hi"])
    client = LLMClient(api_url="https://llm.example.com", http=http, get_token=lambda: "mytoken")
    list(client.stream(messages=[{"role": "user", "content": "q"}], model="m"))
    headers = http.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer mytoken"


def test_stream_timeout_passed():
    http = _mock_streaming_http(["hi"])
    client = LLMClient(
        api_url="https://llm.example.com", http=http, get_token=lambda: "tok", timeout=77
    )
    list(client.stream(messages=[{"role": "user", "content": "q"}], model="m"))
    timeout = http.post.call_args[1]["timeout"]
    assert timeout == 77


def test_stream_retries_3_times_on_error():
    sleep_calls: list[float] = []
    http = MagicMock()

    call_count = [0]

    class _FakeStreamOk:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_lines(self):
            payload = {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}
            yield f"data: {json.dumps(payload)}"
            yield "data: [DONE]"

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise Exception("connection error")
        return _FakeStreamOk()

    http.post.side_effect = side_effect
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: sleep_calls.append(s),
    )
    result = list(client.stream(messages=[{"role": "user", "content": "q"}], model="m"))
    assert result == ["ok"]
    assert http.post.call_count == 3
    assert sleep_calls == [2.0, 2.0]


def test_stream_raises_upstream_service_error_after_3_failures():
    http = MagicMock()
    http.post.side_effect = Exception("fail")
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamServiceError) as exc_info:
        list(client.stream(messages=[{"role": "user", "content": "q"}], model="m"))
    assert exc_info.value.service == "llm"
    assert exc_info.value.error_code == "LLM_ERROR"
    assert exc_info.value.http_status == 502
    assert "fail" in str(exc_info.value)
    assert http.post.call_count == 3


def test_stream_wraps_timeout_as_upstream_timeout_error():
    http = MagicMock()
    http.post.side_effect = httpx.TimeoutException("read timeout")
    client = LLMClient(
        api_url="https://llm.example.com",
        http=http,
        get_token=lambda: "tok",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamTimeoutError) as exc_info:
        list(client.stream(messages=[{"role": "user", "content": "q"}], model="m"))
    assert exc_info.value.error_code == "LLM_TIMEOUT"
    assert exc_info.value.http_status == 504
