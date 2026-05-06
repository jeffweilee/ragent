"""T2v.29 — MinioSiteRegistry: parse `MINIO_SITES` JSON, fail-fast at boot.

A site record exposes (endpoint, auth, bucket, read_only). Inline ingest
always stages to `__default__`; file ingest reads from caller-supplied
`(minio_site, object_key)` and HEAD-probes via `stat_object`. `read_only`
sites suppress post-READY delete unconditionally.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from minio import Minio
from minio.error import S3Error

DEFAULT_SITE = "__default__"
_REQUIRED = ("name", "endpoint", "access_key", "secret_key", "bucket")


class UnknownMinioSite(Exception):
    pass


@dataclass
class SiteRecord:
    name: str
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    secure: bool = False
    read_only: bool = False
    client: Any | None = field(default=None, repr=False)


def _sanitise(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", lambda m: f"%{ord(m.group()):02X}", s)


class MinioSiteRegistry:
    def __init__(self, sites: dict[str, SiteRecord]) -> None:
        self._sites = sites

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        minio_factory: Callable[..., Any] | None = None,
    ) -> MinioSiteRegistry:
        """Boot path. Prefer MINIO_SITES JSON; fall back to legacy single-MinIO
        env vars synthesising a `__default__` entry. Either path enforces the
        `__default__` invariant via from_json().
        """
        import os as _os

        env = env if env is not None else dict(_os.environ)
        sites_raw = env.get("MINIO_SITES", "").strip()
        if sites_raw:
            return cls.from_json(sites_raw, minio_factory=minio_factory)
        endpoint = env.get("MINIO_ENDPOINT")
        access_key = env.get("MINIO_ACCESS_KEY")
        secret_key = env.get("MINIO_SECRET_KEY")
        bucket = env.get("MINIO_BUCKET", "ragent-uploads")
        if not (endpoint and access_key and secret_key):
            raise ValueError(
                "MinIO config missing: set MINIO_SITES (JSON) or "
                "MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY"
            )
        synthesised = json.dumps(
            [
                {
                    "name": DEFAULT_SITE,
                    "endpoint": endpoint,
                    "access_key": access_key,
                    "secret_key": secret_key,
                    "bucket": bucket,
                    "secure": env.get("MINIO_SECURE", "false").lower() == "true",
                    "read_only": False,
                }
            ]
        )
        return cls.from_json(synthesised, minio_factory=minio_factory)

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        minio_factory: Callable[..., Any] | None = None,
    ) -> MinioSiteRegistry:
        try:
            entries = json.loads(raw) if raw else []
        except json.JSONDecodeError as exc:
            raise ValueError(f"MINIO_SITES is not valid JSON: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise ValueError("MINIO_SITES must be a non-empty JSON array")

        factory = minio_factory or _build_minio
        sites: dict[str, SiteRecord] = {}
        for entry in entries:
            for key in _REQUIRED:
                if not entry.get(key):
                    raise ValueError(
                        f"MINIO_SITES entry {entry.get('name')!r} missing field {key!r}"
                    )
            rec = SiteRecord(
                name=entry["name"],
                endpoint=entry["endpoint"],
                access_key=entry["access_key"],
                secret_key=entry["secret_key"],
                bucket=entry["bucket"],
                secure=bool(entry.get("secure", False)),
                read_only=bool(entry.get("read_only", False)),
            )
            rec.client = factory(
                endpoint=rec.endpoint,
                access_key=rec.access_key,
                secret_key=rec.secret_key,
                secure=rec.secure,
            )
            sites[rec.name] = rec

        if DEFAULT_SITE not in sites:
            raise ValueError(f"MINIO_SITES must define a {DEFAULT_SITE!r} site for inline ingest")
        return cls(sites)

    def get(self, name: str) -> SiteRecord:
        try:
            return self._sites[name]
        except KeyError as exc:
            raise UnknownMinioSite(name) from exc

    def default(self) -> SiteRecord:
        return self._sites[DEFAULT_SITE]

    def stat_object(self, site: str, object_key: str) -> int | None:
        rec = self.get(site)
        try:
            stat = rec.client.stat_object(rec.bucket, object_key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                return None
            raise
        return getattr(stat, "size", None)

    def put_object_default(
        self,
        *,
        source_app: str,
        source_id: str,
        document_id: str,
        data: io.IOBase,
        length: int,
        content_type: str,
    ) -> str:
        rec = self.default()
        key = f"{_sanitise(source_app)}_{_sanitise(source_id)}_{document_id}"
        rec.client.put_object(rec.bucket, key, data, length, content_type=content_type)
        return key

    def delete_object(self, site: str, object_key: str) -> None:
        rec = self.get(site)
        if rec.read_only:
            return
        try:
            rec.client.remove_object(rec.bucket, object_key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return
            raise

    def get_object(self, site: str, object_key: str) -> bytes:
        rec = self.get(site)
        resp = rec.client.get_object(rec.bucket, object_key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()


def _build_minio(*, endpoint: str, access_key: str, secret_key: str, secure: bool) -> Minio:
    return Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
