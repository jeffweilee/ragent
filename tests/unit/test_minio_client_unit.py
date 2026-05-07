"""Cover MinIOClient get/delete paths without testcontainers."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from minio.error import S3Error

from ragent.storage.minio_client import MinIOClient


def _client() -> tuple[MinIOClient, MagicMock]:
    inner = MagicMock()
    return MinIOClient(inner, bucket="b", put_timeout=1.0, get_timeout=1.0), inner


def test_put_object_returns_sanitized_key() -> None:
    c, inner = _client()
    key = c.put_object(
        source_app="a/p",
        source_id="s id",
        document_id="DOC",
        data=io.BytesIO(b"x"),
        length=1,
        content_type="text/plain",
    )
    # `/` and space → percent-encoded.
    assert key == "a%2Fp_s%20id_DOC"
    inner.put_object.assert_called_once()


def test_get_object_returns_body_and_closes_response() -> None:
    c, inner = _client()
    resp = MagicMock()
    resp.read.return_value = b"hello"
    inner.get_object.return_value = resp

    assert c.get_object("k") == b"hello"
    resp.close.assert_called_once()
    resp.release_conn.assert_called_once()


def test_delete_object_swallows_no_such_key() -> None:
    c, inner = _client()
    inner.remove_object.side_effect = S3Error(
        code="NoSuchKey",
        message="missing",
        resource="/k",
        request_id="r",
        host_id="h",
        response=MagicMock(),
    )
    # Idempotent: missing key is a no-op.
    c.delete_object("k")


def test_delete_object_reraises_other_s3_errors() -> None:
    c, inner = _client()
    inner.remove_object.side_effect = S3Error(
        code="AccessDenied",
        message="nope",
        resource="/k",
        request_id="r",
        host_id="h",
        response=MagicMock(),
    )
    with pytest.raises(S3Error):
        c.delete_object("k")
