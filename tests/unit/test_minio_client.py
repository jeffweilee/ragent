"""T2.5 — MinIOClient: key format, timeouts, idempotent delete (B10, B25, B28, C3)."""

import io
from unittest.mock import MagicMock

from ragent.storage.minio_client import MinIOClient


def _make_client(bucket="ragent", put_timeout=60, get_timeout=30):
    minio = MagicMock()
    client = MinIOClient(
        minio_client=minio,
        bucket=bucket,
        put_timeout=put_timeout,
        get_timeout=get_timeout,
    )
    return client, minio


def test_put_object_builds_correct_key():
    client, minio = _make_client()
    data = b"hello world"
    key = client.put_object(
        source_app="confluence",
        source_id="DOC-123",
        document_id="AAAAAAAAAAAAAAAAAAAAAAAAAAA",
        data=io.BytesIO(data),
        length=len(data),
        content_type="text/plain",
    )
    assert key == "confluence_DOC-123_AAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_put_object_sanitises_special_chars():
    """source_app / source_id with special chars are sanitised to [A-Za-z0-9._-]."""
    client, minio = _make_client()
    data = b"x"
    key = client.put_object(
        source_app="my app",
        source_id="id/with/slashes",
        document_id="BBBBBBBBBBBBBBBBBBBBBBBBBBB",
        data=io.BytesIO(data),
        length=len(data),
        content_type="text/plain",
    )
    # Special chars replaced; key must not contain raw spaces or slashes
    assert " " not in key
    assert "/" not in key


def test_put_object_returns_only_key_not_uri(monkeypatch):
    """Returns the object key string, not a URI (B25, C3)."""
    client, minio = _make_client()
    data = b"x"
    key = client.put_object(
        source_app="app",
        source_id="sid",
        document_id="CCC",
        data=io.BytesIO(data),
        length=len(data),
        content_type="text/plain",
    )
    assert "://" not in key
    assert key.startswith("app_")


def test_put_object_uses_correct_bucket():
    client, minio = _make_client(bucket="mybucket")
    data = b"x"
    client.put_object(
        source_app="app",
        source_id="sid",
        document_id="DDD",
        data=io.BytesIO(data),
        length=len(data),
        content_type="text/plain",
    )
    call_kwargs = minio.put_object.call_args
    # bucket name should appear in the call
    assert call_kwargs[0][0] == "mybucket" or call_kwargs[1].get("bucket_name") == "mybucket"


def test_delete_object_idempotent_on_missing():
    """delete_object on a non-existent key must NOT raise (idempotent)."""
    client, minio = _make_client()
    # Simulate S3NoSuchKey-like error
    from minio.error import S3Error

    minio.remove_object.side_effect = S3Error(
        code="NoSuchKey",
        message="not found",
        resource="/test",
        request_id="req",
        host_id="host",
        response=MagicMock(status=404, headers={}, text=""),
    )
    # Should not raise
    client.delete_object("some_key")


def test_delete_object_calls_remove_object():
    client, minio = _make_client()
    client.delete_object("app_sid_DOC")
    minio.remove_object.assert_called_once()
    args = minio.remove_object.call_args
    assert "app_sid_DOC" in (args[0] + tuple(args[1].values()))


def test_bucket_read_once_at_startup():
    """Bucket is read from constructor, not per-row (C3)."""
    _, minio = _make_client(bucket="staging-bucket")
    # The bucket name is fixed at construction time; this is verified structurally
    # by inspecting the client, not by calling put_object multiple times.
    client2 = MinIOClient(
        minio_client=minio,
        bucket="staging-bucket",
        put_timeout=60,
        get_timeout=30,
    )
    assert client2._bucket == "staging-bucket"
