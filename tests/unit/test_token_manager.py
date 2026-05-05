"""T4.1 — TokenManager: J1→J2 refresh, boundary clock, single-flight (S9, P-F)."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ragent.clients.auth import TokenManager


def _make_response(token: str, expires_in_seconds: int, now: float) -> dict:
    return {"access_token": token, "expiresAt": int((now + expires_in_seconds) * 1000)}


def test_get_token_fetches_on_first_call():
    now = time.time()
    resp = _make_response("tok-abc", 3600, now)
    http = MagicMock()
    http.post.return_value.json.return_value = resp
    http.post.return_value.raise_for_status = MagicMock()

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="id",
        client_secret="secret",
        http=http,
        clock=lambda: now,
    )
    token = mgr.get_token()
    assert token == "tok-abc"
    assert http.post.call_count == 1
    body = http.post.call_args[1]["json"]
    assert body["clientId"] == "id"
    assert body["clientSecret"] == "secret"


def test_get_token_caches_within_window():
    now = time.time()
    resp = _make_response("tok-cached", 3600, now)
    http = MagicMock()
    http.post.return_value.json.return_value = resp
    http.post.return_value.raise_for_status = MagicMock()

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="id",
        client_secret="secret",
        http=http,
        clock=lambda: now,
    )
    mgr.get_token()
    mgr.get_token()
    assert http.post.call_count == 1


def test_refreshes_at_expiry_boundary():
    """Token is refreshed when wall-clock >= expiresAt - 5min (300 s)."""
    now = time.time()
    # First token expires in 3600 s
    resp1 = _make_response("tok-1", 3600, now)
    # Second token (after refresh) expires in 3600 s from the boundary moment
    boundary = now + 3600 - 300  # exactly at expiresAt − 5min
    resp2 = _make_response("tok-2", 3600, boundary)

    call_count = [0]

    def post_side_effect(*args, **kwargs):
        call_count[0] += 1
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        if call_count[0] == 1:
            mock_resp.json.return_value = resp1
        else:
            mock_resp.json.return_value = resp2
        return mock_resp

    http = MagicMock()
    http.post.side_effect = post_side_effect

    clock = [now]
    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="id",
        client_secret="secret",
        http=http,
        clock=lambda: clock[0],
    )

    assert mgr.get_token() == "tok-1"
    assert http.post.call_count == 1

    # Advance clock to boundary
    clock[0] = boundary
    assert mgr.get_token() == "tok-2"
    assert http.post.call_count == 2


def test_single_flight_100_concurrent_callers():
    """100 concurrent callers at boundary share exactly one HTTP exchange (P-F)."""
    now = time.time()
    # Token already at boundary: expires soon
    boundary = now + 299  # less than 300 s left → needs refresh
    resp = _make_response("tok-shared", 3600, now + 3600)

    http = MagicMock()
    http.post.return_value.json.return_value = resp
    http.post.return_value.raise_for_status = MagicMock()

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="id",
        client_secret="secret",
        http=http,
        clock=lambda: boundary,
    )

    results: list[str] = []
    lock = threading.Lock()

    def call():
        t = mgr.get_token()
        with lock:
            results.append(t)

    threads = [threading.Thread(target=call) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 100
    assert all(r == "tok-shared" for r in results)
    assert http.post.call_count == 1


def test_credentials_not_in_exception_message():
    """Client credentials must not surface in exception text (security)."""
    http = MagicMock()
    http.post.side_effect = Exception("connection refused")

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="secret-id",
        client_secret="super-secret",
        http=http,
        clock=time.time,
    )

    with pytest.raises(Exception) as exc_info:
        mgr.get_token()

    msg = str(exc_info.value)
    assert "secret-id" not in msg
    assert "super-secret" not in msg


def test_posts_to_correct_url():
    now = time.time()
    resp = _make_response("tok", 3600, now)
    http = MagicMock()
    http.post.return_value.json.return_value = resp
    http.post.return_value.raise_for_status = MagicMock()

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        client_id="id",
        client_secret="secret",
        http=http,
        clock=lambda: now,
    )
    mgr.get_token()
    url = http.post.call_args[0][0] if http.post.call_args[0] else http.post.call_args[1].get("url")
    assert "auth/api/accesstoken" in url
