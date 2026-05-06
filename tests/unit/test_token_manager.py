"""T4.1 — TokenManager: J1→J2 refresh, boundary clock, single-flight (S9, P-F).

Spec: docs/00_rule.md §"LLM & Embedding & Re-rank Auth API (Token Exchange)"
- Request body:  {"key": "<j1-token>"}
- Response body: {"token": "<j2-token>", "expiresAt": "2026-01-07T13:20:36Z"}
- K8s mode: reads SA token from /var/run/secrets/kubernetes.io/serviceaccount/token
- Refresh margin: 5 min before expiresAt
- Single-flight: threading.Lock prevents concurrent refresh stampede
"""

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ragent.clients.auth import TokenManager


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_response(token: str, expires_at_ts: float) -> dict:
    return {"token": token, "expiresAt": _iso(expires_at_ts)}


def _make_mgr(j1: str, http: MagicMock, clock) -> TokenManager:
    return TokenManager(
        auth_url="https://auth.example.com",
        j1_token=j1,
        http=http,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Request format
# ---------------------------------------------------------------------------


def test_request_body_uses_key_field():
    """POST body must be {"key": j1_token}, not clientId/clientSecret."""
    now = time.time()
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    _make_mgr("my-j1", http, lambda: now).get_token()

    body = http.post.call_args[1]["json"]
    assert body == {"key": "my-j1"}, f"unexpected body: {body}"


def test_request_posts_to_auth_accesstoken_url():
    """URL must contain auth/api/accesstoken."""
    now = time.time()
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    _make_mgr("j1", http, lambda: now).get_token()

    url = http.post.call_args[0][0] if http.post.call_args[0] else http.post.call_args[1].get("url")
    assert "auth/api/accesstoken" in url


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_response_reads_token_field():
    """Response field is 'token', not 'access_token'."""
    now = time.time()
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok-abc", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    result = _make_mgr("j1", http, lambda: now).get_token()
    assert result == "tok-abc"


def test_response_parses_iso8601_expires_at():
    """expiresAt is ISO-8601 string, not numeric milliseconds."""
    now = time.time()
    # Token expires in exactly 3600 s; clock at now+3299 (still valid, no refresh)
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    mgr = _make_mgr("j1", http, lambda: now)
    mgr.get_token()  # first call fetches
    mgr.get_token()  # second call must use cache (no extra HTTP)
    assert http.post.call_count == 1


# ---------------------------------------------------------------------------
# Cache & refresh boundary
# ---------------------------------------------------------------------------


def test_caches_within_valid_window():
    now = time.time()
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok-cached", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    mgr = _make_mgr("j1", http, lambda: now)
    mgr.get_token()
    mgr.get_token()
    assert http.post.call_count == 1


def test_refreshes_at_5min_boundary():
    """Refresh triggers when wall-clock >= expiresAt - 300 s."""
    now = time.time()
    expires_1 = now + 3600
    boundary = expires_1 - 300  # exactly 5 min before expiry

    call_count = [0]

    def post_side(*_, **__):
        call_count[0] += 1
        m = MagicMock()
        m.raise_for_status = MagicMock()
        if call_count[0] == 1:
            m.json.return_value = _make_response("tok-1", expires_1)
        else:
            m.json.return_value = _make_response("tok-2", boundary + 3600)
        return m

    http = MagicMock()
    http.post.side_effect = post_side

    clock = [now]
    mgr = _make_mgr("j1", http, lambda: clock[0])

    assert mgr.get_token() == "tok-1"
    assert http.post.call_count == 1

    clock[0] = boundary
    assert mgr.get_token() == "tok-2"
    assert http.post.call_count == 2


# ---------------------------------------------------------------------------
# Single-flight (P-F)
# ---------------------------------------------------------------------------


def test_single_flight_100_concurrent_callers():
    """100 concurrent callers at boundary share exactly one HTTP exchange."""
    now = time.time()
    # Token already needs refresh (< 300 s left)
    boundary = now + 299

    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("tok-shared", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    mgr = _make_mgr("j1", http, lambda: boundary)

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


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_j1_token_not_in_exception_message():
    """J1 token must not appear in exception text."""
    http = MagicMock()
    http.post.side_effect = Exception("connection refused")

    mgr = _make_mgr("super-secret-j1", http, time.time)

    with pytest.raises(Exception) as exc_info:
        mgr.get_token()

    assert "super-secret-j1" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Kubernetes service-account mode
# ---------------------------------------------------------------------------


def test_k8s_mode_reads_sa_token_from_file(tmp_path: Path):
    """When j1_token not given, TokenManager reads from SA token file."""
    sa_file = tmp_path / "token"
    sa_file.write_text("sa-token-value")

    now = time.time()
    http = MagicMock()
    http.post.return_value.json.return_value = _make_response("j2-from-sa", now + 3600)
    http.post.return_value.raise_for_status = MagicMock()

    mgr = TokenManager(
        auth_url="https://auth.example.com",
        j1_token=None,
        k8s_sa_token_path=str(sa_file),
        http=http,
        clock=lambda: now,
    )
    result = mgr.get_token()
    assert result == "j2-from-sa"
    body = http.post.call_args[1]["json"]
    assert body == {"key": "sa-token-value"}


def test_k8s_mode_raises_when_sa_file_missing():
    """Missing SA token file raises RuntimeError at refresh time."""
    http = MagicMock()
    mgr = TokenManager(
        auth_url="https://auth.example.com",
        j1_token=None,
        k8s_sa_token_path="/nonexistent/token",
        http=http,
        clock=time.time,
    )
    with pytest.raises(RuntimeError, match="Token refresh failed"):
        mgr.get_token()


def test_k8s_mode_requires_path_when_j1_is_none():
    """Constructing with j1_token=None and no k8s path raises ValueError."""
    with pytest.raises(ValueError):
        TokenManager(
            auth_url="https://auth.example.com",
            j1_token=None,
            http=MagicMock(),
        )
