"""T-CAv3.3 — ragent-side concrete ADKCaller (twp-ai upstream proxy backend)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from twp_ai.schemas import RunAgentInput

from ragent.clients.adk_caller import ADKCaller
from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError


def _request(messages: list[dict] | None = None) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread_1",
            "runId": "run_1",
            "messages": messages
            or [{"id": "m1", "role": "user", "content": "What are the features?"}],
            "tools": [],
            "state": None,
            "context": [],
            "forwardedProps": None,
        }
    )


def _resp_mock(lines: list[bytes]):
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.iter_lines.return_value = iter([line.decode() for line in lines])
    return m


def _make_caller(http_mock, *, user_id="alice", user_token="tok-1"):
    return ADKCaller(
        http_client=http_mock,
        api_url="http://upstream",
        ap_name="TestAP",
        auth="Bearer up",
        user_id=user_id,
        user_token=user_token,
    )


def test_stream_deltas_builds_upstream_payload() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([b'{"returnCode":96200,"returnData":{"done":true}}'])
    caller = _make_caller(http_mock, user_id="bob", user_token="tok-bob")

    list(caller.stream_deltas(_request(), "ignored-model"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok-bob"
    assert payload["metadata"]["session"] == "thread_1"  # session = threadId
    assert payload["inputData"]["message"] == "What are the features?"
    assert payload["stream"] is True


def test_stream_deltas_uses_last_user_message() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([b'{"returnData":{"done":true}}'])
    caller = _make_caller(http_mock)
    messages = [
        {"id": "m1", "role": "user", "content": "first"},
        {"id": "m2", "role": "assistant", "content": "reply"},
        {"id": "m3", "role": "user", "content": "latest question"},
    ]

    list(caller.stream_deltas(_request(messages), "m"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"]["message"] == "latest question"


def test_stream_deltas_yields_deltas_until_done() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [
            b'{"returnCode":96200,"returnData":{"delta":"The "}}',
            b'{"returnCode":96200,"returnData":{"delta":"features"}}',
            b'{"returnCode":96200,"returnData":{"done":true}}',
            b'{"returnCode":96200,"returnData":{"delta":"after done ignored"}}',
        ]
    )
    caller = _make_caller(http_mock)

    deltas = list(caller.stream_deltas(_request(), "m"))

    assert deltas == ["The ", "features"]


def test_stream_deltas_timeout_raises_timeout_error() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.side_effect = httpx.TimeoutException("timed out")
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamTimeoutError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_TIMEOUT


def test_stream_deltas_request_error_raises_service_error() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.side_effect = httpx.RequestError("conn refused")
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamServiceError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_stream_deltas_non_96200_raises_service_error() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [b'{"returnCode":96500,"returnData":{"message":"upstream rejected"}}']
    )
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamServiceError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR
