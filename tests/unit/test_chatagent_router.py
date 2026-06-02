"""T-CA.R1–R3 — chatagent router unit tests (POST + GET sessionList + GET session)."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    """Build a minimal unsigned JWT stub for testing payload decode."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.sig"


def _make_app(
    *,
    chatagent_api_url: str | None = "http://chatagent",
    chatagent_sessionlist_api_url: str | None = "http://sessionlist",
    chatagent_session_api_url: str | None = "http://session",
    chatagent_ap_name: str = "TestAP",
    chatagent_auth: str | None = None,
    http_mock: Any = None,
    rate_limiter: Any = None,
    jwt_header: str = "X-Auth-Token",
) -> FastAPI:
    from ragent.routers.chatagent import create_chatagent_router

    if http_mock is None:
        http_mock = MagicMock()

    app = FastAPI()
    router = create_chatagent_router(
        http_client=http_mock,
        chatagent_ap_name=chatagent_ap_name,
        chatagent_auth=chatagent_auth,
        chatagent_api_url=chatagent_api_url,
        chatagent_sessionlist_api_url=chatagent_sessionlist_api_url,
        chatagent_session_api_url=chatagent_session_api_url,
        rate_limiter=rate_limiter,
        jwt_header=jwt_header,
    )
    app.include_router(router)
    return app


def _post_response(content: str = "Hello!") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "returnCode": 96200,
        "returnData": {"messages": [{"role": "assistant", "content": content, "message_id": "m1"}]},
    }
    return resp


def _get_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = body
    return resp


# ===========================================================================
# POST /chatagent/v1
# ===========================================================================


def test_post_returns_200_with_chat_shape():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response("World!")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        resp = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "World!"
    assert body["usage"] == {"promptTokens": None, "completionTokens": None}
    assert "model" in body
    assert "provider" in body
    assert body["sources"] is None


def test_post_session_from_body_used():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}], "session": "my-sess"},
            headers={"X-User-Id": "alice"},
        )
    call_kwargs = http_mock.post.call_args
    payload = call_kwargs[1]["json"]
    assert payload["metadata"]["session"] == "my-sess"


def test_post_session_generated_when_absent():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    session_val = payload["metadata"]["session"]
    assert isinstance(session_val, str) and len(session_val) > 0


def test_post_sub_extracted_from_jwt():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock, jwt_header="X-Auth-Token")
    token = _make_jwt({"sub": "user-sub-123", "iss": "test"})
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice", "X-Auth-Token": token},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["metadata"]["user"] == "user-sub-123"


def test_post_sub_falls_back_to_user_id():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "fallback-user"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["metadata"]["user"] == "fallback-user"


def test_post_user_token_is_raw_jwt():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock, jwt_header="X-Auth-Token")
    token = _make_jwt({"sub": "u1"})
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice", "X-Auth-Token": token},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["metadata"]["userToken"] == token


def test_post_user_token_empty_when_no_jwt():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["metadata"]["userToken"] == ""


def test_post_ap_name_in_metadata():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock, chatagent_ap_name="MyApp")
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["metadata"]["apName"] == "MyApp"


def test_post_stream_false_in_payload():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["stream"] is False


def test_post_last_user_message_extracted():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply"},
                    {"role": "user", "content": "last question"},
                ]
            },
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["inputData"]["message"] == "last question"


def test_post_auth_header_included_when_configured():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock, chatagent_auth="Basic abc123")
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    headers_sent = http_mock.post.call_args[1]["headers"]
    assert headers_sent.get("Authorization") == "Basic abc123"


def test_post_auth_header_omitted_when_not_configured():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock, chatagent_auth=None)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    headers_sent = http_mock.post.call_args[1]["headers"]
    assert "Authorization" not in headers_sent


def test_post_non_96200_return_code_gives_502():
    http_mock = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"returnCode": 96500, "returnData": {"messages": []}}
    http_mock.post.return_value = resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_empty_messages_array_gives_502():
    http_mock = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"returnCode": 96200, "returnData": {"messages": []}}
    http_mock.post.return_value = resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_absent_return_data_gives_502():
    http_mock = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"returnCode": 96200}
    http_mock.post.return_value = resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_no_user_role_message_sends_empty_string():
    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "assistant", "content": "I am a bot"}]},
            headers={"X-User-Id": "alice"},
        )
    payload = http_mock.post.call_args[1]["json"]
    assert payload["inputData"]["message"] == ""


def test_post_upstream_http_error_gives_502():
    http_mock = MagicMock()
    http_mock.post.return_value = MagicMock(
        raise_for_status=MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
    )
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_timeout_gives_504():
    http_mock = MagicMock()
    http_mock.post.side_effect = httpx.TimeoutException("timeout")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


def test_post_connect_error_gives_502():
    http_mock = MagicMock()
    http_mock.post.side_effect = httpx.ConnectError("refused")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_malformed_json_gives_502():
    http_mock = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.side_effect = ValueError("not json")
    http_mock.post.return_value = mock_resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_non_dict_json_gives_502():
    http_mock = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"returnCode": 96200}]
    http_mock.post.return_value = mock_resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_post_rate_limit_returns_429():
    from ragent.clients.rate_limiter import RateLimitResult

    rate_limiter = MagicMock()
    rate_limiter.check.return_value = RateLimitResult(allowed=False, remaining=0, reset_at=9999.0)
    http_mock = MagicMock()
    app = _make_app(http_mock=http_mock, rate_limiter=rate_limiter)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 429
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
    assert "Retry-After" in r.headers
    http_mock.post.assert_not_called()


def test_post_missing_messages_gives_422():
    app = _make_app()
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 422


# ===========================================================================
# GET /chatagent/v1/sessionList
# ===========================================================================


def test_session_list_returns_upstream_json():
    upstream_body = {"totalCount": 2, "sessions": [{"session": "s1"}, {"session": "s2"}]}
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response(upstream_body)
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.json() == upstream_body


def test_session_list_forwards_user_and_apname():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response({"totalCount": 0, "sessions": []})
    app = _make_app(http_mock=http_mock, chatagent_ap_name="MyAP")
    with TestClient(app) as client:
        client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "bob"})
    params = http_mock.get.call_args[1]["params"]
    assert params["user"] == "bob"
    assert params["apName"] == "MyAP"


def test_session_list_forwards_start_end_time():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response({"totalCount": 0, "sessions": []})
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.get(
            "/chatagent/v1/sessionList?startTime=2025-01-01T00:00:00.000Z&endTime=2025-12-31T23:59:59.999Z",
            headers={"X-User-Id": "alice"},
        )
    params = http_mock.get.call_args[1]["params"]
    assert params["startTime"] == "2025-01-01T00:00:00.000Z"
    assert params["endTime"] == "2025-12-31T23:59:59.999Z"


def test_session_list_omits_time_params_when_absent():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response({"totalCount": 0, "sessions": []})
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    params = http_mock.get.call_args[1]["params"]
    assert "startTime" not in params
    assert "endTime" not in params


def test_session_list_only_start_time_forwarded():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response({"totalCount": 0, "sessions": []})
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        client.get(
            "/chatagent/v1/sessionList?startTime=2025-01-01T00:00:00.000Z",
            headers={"X-User-Id": "alice"},
        )
    params = http_mock.get.call_args[1]["params"]
    assert params["startTime"] == "2025-01-01T00:00:00.000Z"
    assert "endTime" not in params


def test_session_list_auth_header_forwarded():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response({"totalCount": 0, "sessions": []})
    app = _make_app(http_mock=http_mock, chatagent_auth="Basic xyz")
    with TestClient(app) as client:
        client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    headers_sent = http_mock.get.call_args[1]["headers"]
    assert headers_sent.get("Authorization") == "Basic xyz"


def test_session_list_timeout_gives_504():
    http_mock = MagicMock()
    http_mock.get.side_effect = httpx.TimeoutException("timeout")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


def test_session_list_upstream_error_gives_502():
    http_mock = MagicMock()
    http_mock.get.return_value = MagicMock(
        raise_for_status=MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
    )
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_session_list_connect_error_gives_502():
    http_mock = MagicMock()
    http_mock.get.side_effect = httpx.ConnectError("refused")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_session_list_malformed_json_gives_502():
    http_mock = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.side_effect = ValueError("not json")
    http_mock.get.return_value = mock_resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/sessionList", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


# ===========================================================================
# GET /chatagent/v1/session
# ===========================================================================

_SESSION_DETAIL = {
    "_id": "abc",
    "apName": "TestAP",
    "user": "alice",
    "session": "s1",
    "sessionName": "chat",
    "sessionStatus": "active",
    "messages": [],
    "createTime": "2025-05-01T00:00:00.000Z",
    "updateTime": "2025-05-01T00:00:00.000Z",
}


def test_session_returns_upstream_json():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response(_SESSION_DETAIL)
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.json() == _SESSION_DETAIL


def test_session_forwards_user_apname_and_session():
    http_mock = MagicMock()
    http_mock.get.return_value = _get_response(_SESSION_DETAIL)
    app = _make_app(http_mock=http_mock, chatagent_ap_name="TheAP")
    with TestClient(app) as client:
        client.get("/chatagent/v1/session?session=my-session", headers={"X-User-Id": "bob"})
    params = http_mock.get.call_args[1]["params"]
    assert params["user"] == "bob"
    assert params["apName"] == "TheAP"
    assert params["session"] == "my-session"


def test_session_required_param_missing_gives_422():
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session", headers={"X-User-Id": "alice"})
    assert r.status_code == 422


def test_session_timeout_gives_504():
    http_mock = MagicMock()
    http_mock.get.side_effect = httpx.TimeoutException("timeout")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


def test_session_upstream_error_gives_502():
    http_mock = MagicMock()
    http_mock.get.return_value = MagicMock(
        raise_for_status=MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
    )
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_session_connect_error_gives_502():
    http_mock = MagicMock()
    http_mock.get.side_effect = httpx.ConnectError("refused")
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_session_malformed_json_gives_502():
    http_mock = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.side_effect = ValueError("not json")
    http_mock.get.return_value = mock_resp
    app = _make_app(http_mock=http_mock)
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


# ===========================================================================
# Conditional route registration
# ===========================================================================


def test_router_not_registered_when_no_urls():
    from ragent.routers.chatagent import create_chatagent_router

    http_mock = MagicMock()
    app = FastAPI()
    router = create_chatagent_router(
        http_client=http_mock,
        chatagent_ap_name="AP",
        chatagent_api_url=None,
        chatagent_sessionlist_api_url=None,
        chatagent_session_api_url=None,
    )
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post("/chatagent/v1", json={}).status_code == 404
        assert client.get("/chatagent/v1/sessionList").status_code == 404
        assert client.get("/chatagent/v1/session").status_code == 404


def test_only_post_registered_when_only_chatagent_url_set():
    from ragent.routers.chatagent import create_chatagent_router

    http_mock = MagicMock()
    http_mock.post.return_value = _post_response()
    app = FastAPI()
    router = create_chatagent_router(
        http_client=http_mock,
        chatagent_ap_name="AP",
        chatagent_api_url="http://chatagent",
        chatagent_sessionlist_api_url=None,
        chatagent_session_api_url=None,
    )
    app.include_router(router)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
        assert r.status_code == 200
        assert client.get("/chatagent/v1/sessionList").status_code == 404
        assert client.get("/chatagent/v1/session").status_code == 404
