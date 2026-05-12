"""T2v.28 — MinioSiteRegistry: parse MINIO_SITES, fail-fast, HEAD probe (spec §8)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from minio.error import S3Error

from ragent.storage.minio_registry import (
    MinioSiteRegistry,
    UnknownMinioSite,
)


def _site(name="__default__", bucket="b", read_only=False):
    return {
        "name": name,
        "endpoint": "minio:9000",
        "access_key": "ak",
        "secret_key": "sk",
        "bucket": bucket,
        "read_only": read_only,
    }


def _factory(stub):
    """Returns a Minio() factory that yields the given stub for all sites."""
    return lambda **_: stub


def test_boot_fails_when_default_missing():
    raw = json.dumps([_site(name="tenant-eu-1")])
    with pytest.raises(ValueError, match="__default__"):
        MinioSiteRegistry.from_json(raw, minio_factory=_factory(MagicMock()))


def test_boot_fails_on_empty_or_invalid_json():
    with pytest.raises(ValueError):
        MinioSiteRegistry.from_json("", minio_factory=_factory(MagicMock()))
    with pytest.raises(ValueError):
        MinioSiteRegistry.from_json("[]", minio_factory=_factory(MagicMock()))


def test_boot_fails_when_site_missing_required_fields():
    raw = json.dumps([{"name": "__default__", "endpoint": "x", "bucket": "b"}])
    with pytest.raises(ValueError, match="access_key|secret_key"):
        MinioSiteRegistry.from_json(raw, minio_factory=_factory(MagicMock()))


def test_get_returns_site_record_with_bucket_and_read_only():
    raw = json.dumps([_site(), _site(name="tenant-eu-1", bucket="eu", read_only=True)])
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(MagicMock()))
    rec = reg.get("tenant-eu-1")
    assert rec.bucket == "eu"
    assert rec.read_only is True
    assert reg.get("__default__").read_only is False


def test_get_unknown_raises():
    raw = json.dumps([_site()])
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(MagicMock()))
    with pytest.raises(UnknownMinioSite):
        reg.get("does-not-exist")


def test_default_helper_returns_default_record():
    raw = json.dumps([_site()])
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(MagicMock()))
    assert reg.default().name == "__default__"


def test_clients_cached_per_site():
    raw = json.dumps([_site(), _site(name="tenant-eu-1")])
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return MagicMock()

    reg = MinioSiteRegistry.from_json(raw, minio_factory=factory)
    reg.get("__default__")
    reg.get("__default__")
    reg.get("tenant-eu-1")
    # one client per site, regardless of get() count
    assert len(calls) == 2


def test_stat_object_returns_size_or_none():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.stat_object.return_value = MagicMock(size=42)
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    assert reg.stat_object("__default__", "key") == 42


def test_stat_object_returns_none_when_missing():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.stat_object.side_effect = S3Error(
        code="NoSuchKey",
        message="not found",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(status=404, headers={}, text=""),
    )
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    assert reg.stat_object("__default__", "missing") is None


def test_put_object_routes_through_default_site():
    raw = json.dumps([_site()])
    stub = MagicMock()
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    import io

    key = reg.put_object_default(
        source_app="app",
        source_id="sid",
        document_id="DOC",
        data=io.BytesIO(b"x"),
        length=1,
        content_type="text/plain",
    )
    assert key == "app_sid_DOC"
    stub.put_object.assert_called_once()


def test_delete_object_skips_read_only_site():
    raw = json.dumps([_site(), _site(name="caller", read_only=True)])
    stub = MagicMock()
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    reg.delete_object("caller", "any-key")
    stub.remove_object.assert_not_called()


def test_delete_object_swallows_no_such_key():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.remove_object.side_effect = S3Error(
        code="NoSuchKey",
        message="missing",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(status=404, headers={}, text=""),
    )
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    reg.delete_object("__default__", "vanished")  # must not raise


def test_delete_object_propagates_other_s3_errors():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.remove_object.side_effect = S3Error(
        code="AccessDenied",
        message="nope",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(status=403, headers={}, text=""),
    )
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    with pytest.raises(S3Error):
        reg.delete_object("__default__", "key")


def test_head_object_returns_size_and_content_type():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.stat_object.return_value = MagicMock(size=1024, content_type="text/plain")
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    result = reg.head_object("__default__", "key")
    assert result == (1024, "text/plain")


def test_head_object_returns_none_when_object_missing():
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.stat_object.side_effect = S3Error(
        code="NoSuchKey",
        message="missing",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(status=404, headers={}, text=""),
    )
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    assert reg.head_object("__default__", "missing") is None


def test_head_object_preserves_none_size_so_worker_skips_size_check():
    """When MinIO stat returns size=None the worker must receive None (not 0)
    so it skips size-mismatch validation instead of failing every non-empty file.
    Regression guard for the `or 0` bug in head_object."""
    raw = json.dumps([_site()])
    stub = MagicMock()
    stub.stat_object.return_value = MagicMock(size=None, content_type="application/octet-stream")
    reg = MinioSiteRegistry.from_json(raw, minio_factory=_factory(stub))
    size, ct = reg.head_object("__default__", "key")
    assert size is None, "None size must pass through so worker uses expected_size=None"
    assert ct == "application/octet-stream"
