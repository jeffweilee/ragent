"""T-UP.1 — UnprotectClient: multipart POST contract."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest


def _make_client(*, apikey: str = "jwt-tok", suffix: str = "@corp.example"):
    from ragent.clients.unprotect import UnprotectClient

    http = MagicMock(spec=httpx.Client)
    return UnprotectClient(
        api_url="https://unprotect.example/api/unprotect",
        apikey=apikey,
        delegated_user_suffix=suffix,
        http=http,
    ), http


def test_unprotect_returns_response_bytes():
    client, http = _make_client()
    raw = b"decrypted-content"
    mock_resp = MagicMock()
    mock_resp.content = raw
    mock_resp.raise_for_status = MagicMock()
    http.post.return_value = mock_resp

    result = client.unprotect(file_bytes=b"encrypted", user_id="user-42", filename="doc.pdf")

    assert result == raw


def test_unprotect_posts_to_correct_url():
    client, http = _make_client()
    mock_resp = MagicMock()
    mock_resp.content = b"ok"
    mock_resp.raise_for_status = MagicMock()
    http.post.return_value = mock_resp

    client.unprotect(file_bytes=b"bytes", user_id="u1", filename="f.docx")

    url = http.post.call_args[0][0]
    assert url == "https://unprotect.example/api/unprotect"


def test_unprotect_sends_apikey_header():
    client, http = _make_client(apikey="secret-jwt")
    mock_resp = MagicMock()
    mock_resp.content = b"ok"
    mock_resp.raise_for_status = MagicMock()
    http.post.return_value = mock_resp

    client.unprotect(file_bytes=b"bytes", user_id="u1", filename="f")

    headers = http.post.call_args[1]["headers"]
    assert headers.get("apikey") == "secret-jwt"


def test_unprotect_delegated_user_is_user_id_plus_suffix():
    client, http = _make_client(suffix="@example.org")
    mock_resp = MagicMock()
    mock_resp.content = b"ok"
    mock_resp.raise_for_status = MagicMock()
    http.post.return_value = mock_resp

    client.unprotect(file_bytes=b"bytes", user_id="alice", filename="f")

    data = http.post.call_args[1]["data"]
    assert data["delegatedUser"] == "alice@example.org"


def test_unprotect_sends_file_bytes_as_fileInput():
    client, http = _make_client()
    payload = b"raw-binary-content"
    mock_resp = MagicMock()
    mock_resp.content = b"ok"
    mock_resp.raise_for_status = MagicMock()
    http.post.return_value = mock_resp

    client.unprotect(file_bytes=payload, user_id="u1", filename="report.pptx")

    files = http.post.call_args[1]["files"]
    name, sent_bytes = files["fileInput"]
    assert name == "report.pptx"
    assert sent_bytes == payload


def test_unprotect_raises_on_http_error():
    client, http = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403", request=MagicMock(), response=MagicMock()
    )
    http.post.return_value = mock_resp

    with pytest.raises(httpx.HTTPStatusError):
        client.unprotect(file_bytes=b"bytes", user_id="u1", filename="f")
