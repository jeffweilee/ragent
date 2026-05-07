"""MinIO get_object size verification — partial-read truncation must surface."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ragent.storage.minio_registry import MinioSiteRegistry


def _site(name: str = "__default__") -> dict:
    return {
        "name": name,
        "endpoint": "minio:9000",
        "access_key": "ak",
        "secret_key": "sk",
        "bucket": "b",
        "read_only": False,
    }


def _registry(stub: MagicMock) -> MinioSiteRegistry:
    return MinioSiteRegistry.from_json(json.dumps([_site()]), minio_factory=lambda **_: stub)


def _resp(data: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = data
    return resp


def test_get_object_passes_when_size_matches() -> None:
    stub = MagicMock()
    stub.get_object.return_value = _resp(b"hello world")
    out = _registry(stub).get_object("__default__", "k", expected_size=11)
    assert out == b"hello world"


def test_get_object_raises_on_size_mismatch() -> None:
    stub = MagicMock()
    stub.get_object.return_value = _resp(b"hello")  # 5 bytes
    reg = _registry(stub)
    with pytest.raises(OSError, match="size mismatch"):
        reg.get_object("__default__", "k", expected_size=11)


def test_get_object_without_expected_size_skips_check() -> None:
    """Backward compat: callers that didn't supply expected_size keep working."""
    stub = MagicMock()
    stub.get_object.return_value = _resp(b"hello")
    out = _registry(stub).get_object("__default__", "k")
    assert out == b"hello"
