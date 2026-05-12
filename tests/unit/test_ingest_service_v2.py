"""T2v.26 — IngestService.create v2: discriminated dispatch (inline vs file)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.schemas.ingest import FileIngestRequest, InlineIngestRequest
from ragent.services.ingest_service import (
    FileTooLarge,
    IngestService,
    ObjectNotFoundError,
    UnknownMinioSiteError,
)


def _registry(
    *, default_put_key="app_sid_DOC", stat_size: int | None = 100, sites=("__default__",)
):
    from ragent.storage.minio_registry import UnknownMinioSite

    reg = MagicMock()
    reg.put_object_default.return_value = default_put_key

    def _head(site, _key):
        if site not in sites:
            raise UnknownMinioSite(site)
        # None stat_size means object not found (head_object returns None)
        if stat_size is None:
            return None
        return (stat_size, None)

    reg.head_object.side_effect = _head

    def _get(name):
        if name in sites:
            r = MagicMock()
            r.name = name
            r.read_only = name != "__default__"
            return r
        raise UnknownMinioSite(name)

    reg.get.side_effect = _get
    return reg


def _service(repo=None, broker=None, registry=None):
    repo = repo or AsyncMock()
    broker = broker or AsyncMock()
    registry = registry or _registry()
    svc = IngestService(repo=repo, storage=registry, broker=broker, registry=MagicMock())
    return svc, repo, registry, broker


def _inline(**over):
    base = dict(
        ingest_type="inline",
        source_id="DOC-1",
        source_app="app",
        source_title="T",
        mime_type="text/markdown",
        content="# H1\nbody\n",
    )
    base.update(over)
    return InlineIngestRequest(**base)


def _file(**over):
    base = dict(
        ingest_type="file",
        source_id="DOC-2",
        source_app="s3",
        source_title="T",
        mime_type="text/html",
        minio_site="tenant-eu-1",
        object_key="reports/2025.html",
    )
    base.update(over)
    return FileIngestRequest(**base)


async def test_inline_stages_to_default_and_records_inline_type():
    svc, repo, registry, broker = _service(registry=_registry(sites=("__default__",)))
    doc_id = await svc.create(create_user="alice", request=_inline())
    assert len(doc_id) == 26
    registry.put_object_default.assert_called_once()
    repo.create.assert_called_once()
    kwargs = repo.create.call_args.kwargs
    assert kwargs["ingest_type"] == "inline"
    assert kwargs["minio_site"] is None
    broker.enqueue.assert_called_once()


async def test_file_records_caller_minio_site_without_copy():
    reg = _registry(sites=("__default__", "tenant-eu-1"))
    svc, repo, _, broker = _service(registry=reg)
    await svc.create(create_user="alice", request=_file())
    reg.put_object_default.assert_not_called()
    kwargs = repo.create.call_args.kwargs
    assert kwargs["ingest_type"] == "file"
    assert kwargs["minio_site"] == "tenant-eu-1"
    assert kwargs["object_key"] == "reports/2025.html"


async def test_file_unknown_site_raises():
    reg = _registry(sites=("__default__",))
    svc, repo, _, _ = _service(registry=reg)
    with pytest.raises(UnknownMinioSiteError):
        await svc.create(create_user="alice", request=_file())
    repo.create.assert_not_called()


async def test_file_head_probe_miss_raises():
    reg = _registry(sites=("__default__", "tenant-eu-1"), stat_size=None)
    svc, repo, _, _ = _service(registry=reg)
    with pytest.raises(ObjectNotFoundError):
        await svc.create(create_user="alice", request=_file())
    repo.create.assert_not_called()


async def test_file_too_large_raises_413():
    """spec §4.2: file HEAD-probe size > INGEST_FILE_MAX_BYTES → FileTooLarge (413)."""
    reg = _registry(sites=("__default__", "tenant-eu-1"), stat_size=52428801)
    svc, repo, _, _ = _service(registry=reg)
    with pytest.raises(FileTooLarge):
        await svc.create(create_user="alice", request=_file(), max_file_bytes=52428800)
    repo.create.assert_not_called()


async def test_inline_too_large_raises():
    svc, _, _, _ = _service()
    long_content = "x" * 1024
    with pytest.raises(FileTooLarge):
        await svc.create(
            create_user="alice", request=_inline(content=long_content), max_inline_bytes=10
        )


async def test_inline_persists_source_url_and_workspace():
    svc, repo, _, _ = _service()
    await svc.create(
        create_user="alice",
        request=_inline(source_url="https://x/y", source_meta="eng"),
    )
    kwargs = repo.create.call_args.kwargs
    assert kwargs["source_url"] == "https://x/y"
    assert kwargs["source_meta"] == "eng"


async def test_inline_dispatches_pipeline_task():
    svc, _, _, broker = _service()
    doc_id = await svc.create(create_user="a", request=_inline())
    broker.enqueue.assert_called_once()
    # broker call must carry document_id
    call = broker.enqueue.call_args
    assert doc_id in str(call)


async def test_create_emits_business_log_event():
    """Use structlog.testing.capture_logs (00_rule.md §Service Boundary Logs)
    — capsys misses logger handler output."""
    import structlog

    svc, _, _, _ = _service()
    with structlog.testing.capture_logs() as logs:
        await svc.create(create_user="alice", request=_inline())
    events = [e["event"] for e in logs]
    assert "ingest.received" in events, f"expected ingest.received, got {events}"
