"""T2.6 — MinIOClient: key format B10, returns key-only B25, idempotent delete (C3)."""

from __future__ import annotations

import io
import re
from typing import Any

from minio.error import S3Error


def _sanitise(s: str) -> str:
    """Replace chars outside [A-Za-z0-9._-] with percent-encoded form."""
    return re.sub(r"[^A-Za-z0-9._-]", lambda m: f"%{ord(m.group()):02X}", s)


class MinIOClient:
    def __init__(
        self,
        minio_client: Any,
        bucket: str,
        put_timeout: float,
        get_timeout: float,
    ) -> None:
        self._client = minio_client
        self._bucket = bucket
        self._put_timeout = put_timeout
        self._get_timeout = get_timeout

    def put_object(
        self,
        source_app: str,
        source_id: str,
        document_id: str,
        data: io.IOBase,
        length: int,
        content_type: str,
    ) -> str:
        key = f"{_sanitise(source_app)}_{_sanitise(source_id)}_{document_id}"
        self._client.put_object(
            self._bucket,
            key,
            data,
            length,
            content_type=content_type,
        )
        return key

    def get_object(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete_object(self, key: str) -> None:
        try:
            self._client.remove_object(self._bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return
            raise
