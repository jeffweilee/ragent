"""T-EM.20 — End-to-end embedding-model lifecycle (B50).

Real MariaDB + Elasticsearch testcontainers exercising the lifecycle from
``promote`` through ``cutover``, ``rollback``, ``abort`` and the reconciler
``retired-field`` sweep. Asserts the invariants that the design doc
(``docs/team/2026_05_15_embedding_model_lifecycle.md``) calls out:

- promote PUTs the new ES dense_vector field and records ``embedding.candidate``
- cutover flips ``embedding.read`` to ``"candidate"`` only when preflight passes
- rollback flips read back without touching ``embedding.candidate``
  (dual-write window stays open)
- doc upserts during the rollback window keep BOTH vector fields current
  → rollback after the upsert finds the correct stable vector in ES
- abort retires the candidate and the reconciler sweep zeroes out the
  field values via ``_update_by_query``

The real ingest pipeline is bypassed; we exercise the settings + ES seams
directly. Worker integration is covered by the existing
``test_pipeline_*`` integration suite which now uses the same composition
root (T-EM.21).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lifecycle_dsn():
    """Dedicated MariaDB container so this module's alembic upgrade is
    isolated from the shared session-scoped DB (other tests may have
    already applied ALTERs that alembic upgrade would conflict with —
    same approach as `test_schema_drift.py`)."""
    import os

    from alembic import command
    from alembic.config import Config
    from testcontainers.mysql import MySqlContainer

    from tc_utils import tc_image

    with MySqlContainer(
        image=tc_image("mariadb:10.6"),
        username="u",
        password="p",
        dbname="lifecycle",
    ) as c:
        host = c.get_container_host_ip()
        port = c.get_exposed_port(3306)
        sync_dsn = f"mysql+pymysql://u:p@{host}:{port}/lifecycle?charset=utf8mb4"
        async_dsn = f"mysql+aiomysql://u:p@{host}:{port}/lifecycle?charset=utf8mb4"

        cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        old = os.environ.get("MARIADB_DSN")
        os.environ["MARIADB_DSN"] = sync_dsn
        try:
            command.upgrade(cfg, "head")
        finally:
            if old is None:
                os.environ.pop("MARIADB_DSN", None)
            else:
                os.environ["MARIADB_DSN"] = old

        yield async_dsn


@pytest.fixture
async def lifecycle_engine(lifecycle_dsn):
    """Async SQLAlchemy engine — function-scoped so each test's connections
    bind to its own asyncio loop (`pytest-asyncio` default loop scope =
    function). Sharing an AsyncEngine across event loops raises
    ``RuntimeError('Event loop is closed')`` on the second test."""
    engine = create_async_engine(lifecycle_dsn, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def chunks_index(es_url):
    """Fresh chunks_v1 index per test with only the stable embedding field.

    Promote() adds the candidate field to this mapping at runtime.
    """
    from elasticsearch import AsyncElasticsearch

    es = AsyncElasticsearch(hosts=[es_url])
    index = "chunks_v1"
    if await es.indices.exists(index=index):
        await es.indices.delete(index=index)
    await es.indices.create(
        index=index,
        body={
            "mappings": {
                "properties": {
                    "document_id": {"type": "keyword"},
                    "chunk_id": {"type": "keyword"},
                    "text": {"type": "text"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": 1024,
                        "index": True,
                        "similarity": "cosine",
                        "index_options": {"type": "flat"},
                    },
                }
            }
        },
    )
    yield es, index
    await es.indices.delete(index=index, ignore_unavailable=True)
    await es.close()


@pytest.fixture
async def lifecycle_setup(lifecycle_engine, chunks_index):
    """Wire SystemSettingsRepository + ActiveModelRegistry + LifecycleService
    against the real MariaDB / ES containers."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository
    from ragent.services.active_model_registry import ActiveModelRegistry
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    es, index = chunks_index

    # Reset settings to IDLE state for each test (the migration seeds
    # candidate=null / read=stable already, but the previous test might
    # have transitioned to CUTOVER).
    settings = SystemSettingsRepository(engine=lifecycle_engine)
    await settings.set("embedding.candidate", None)
    await settings.set("embedding.read", "stable")
    await settings.set("embedding.retired", [])

    registry = ActiveModelRegistry(settings_repo=settings, ttl_seconds=1)
    await registry.refresh()

    service = EmbeddingLifecycleService(
        settings_repo=settings,
        es_client=es,
        index_name=index,
        registry=registry,
        cache_ttl_seconds=0,  # disable warmup gate for tests — no time advance
    )
    return {
        "settings": settings,
        "registry": registry,
        "service": service,
        "es": es,
        "index": index,
    }


# ---------------------------------------------------------------------------
# Promote → mapping + settings
# ---------------------------------------------------------------------------


async def test_promote_adds_es_field_and_writes_candidate_setting(lifecycle_setup) -> None:
    svc = lifecycle_setup["service"]
    es = lifecycle_setup["es"]
    settings = lifecycle_setup["settings"]
    index = lifecycle_setup["index"]

    result = await svc.promote(
        name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2"
    )

    assert result["state"] == "CANDIDATE"
    mapping = await es.indices.get_mapping(index=index)
    props = mapping[index]["mappings"]["properties"]
    assert "embedding_bgem3v2_768" in props
    assert props["embedding_bgem3v2_768"]["dims"] == 768

    stored = await settings.get("embedding.candidate")
    assert stored["name"] == "bge-m3-v2"
    assert stored["field"] == "embedding_bgem3v2_768"


# ---------------------------------------------------------------------------
# Cutover → rollback → abort
# ---------------------------------------------------------------------------


async def test_full_lifecycle_promote_cutover_rollback_abort(lifecycle_setup) -> None:
    svc = lifecycle_setup["service"]
    settings = lifecycle_setup["settings"]
    registry = lifecycle_setup["registry"]

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh()

    # Cutover — preflight passes for an empty index (coverage check escapes
    # the threshold), warmup gate disabled via cache_ttl_seconds=0.
    cutover_result = await svc.cutover(force=True)
    assert cutover_result["state"] == "CUTOVER"
    assert await settings.get("embedding.read") == "candidate"

    await registry.refresh()
    rollback_result = await svc.rollback()
    assert rollback_result["state"] == "CANDIDATE"
    assert await settings.get("embedding.read") == "stable"
    # Dual-write window invariant: candidate is still set so future ingests
    # keep updating both vector fields.
    assert (await settings.get("embedding.candidate")) is not None

    await registry.refresh()
    abort_result = await svc.abort()
    assert abort_result["state"] == "IDLE"
    assert await settings.get("embedding.candidate") is None
    retired = await settings.get("embedding.retired")
    assert len(retired) == 1
    assert retired[0]["name"] == "bge-m3-v2"
    assert retired[0]["cleanup_done"] is False


# ---------------------------------------------------------------------------
# Rollback-window write safety
# ---------------------------------------------------------------------------


async def test_doc_upsert_during_rollback_window_keeps_both_fields_current(
    lifecycle_setup,
) -> None:
    """Core B50 safety claim: a chunk written while we're in CANDIDATE /
    CUTOVER state must carry BOTH the stable and candidate vector. Then,
    even after a rollback, the stable vector retrieved from ES matches
    what the most-recent ingest computed."""
    svc = lifecycle_setup["service"]
    es = lifecycle_setup["es"]
    index = lifecycle_setup["index"]
    registry = lifecycle_setup["registry"]

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh()
    await svc.cutover(force=True)
    await registry.refresh()

    # Simulate an ingest landing during CUTOVER: a chunk with BOTH vectors.
    stable_vec = [0.1] * 1024
    cand_vec = [0.9] * 768
    await es.index(
        index=index,
        id="chunk-during-cutover",
        document={
            "document_id": "DOC-XYZ",
            "chunk_id": "chunk-during-cutover",
            "text": "hello",
            "embedding": stable_vec,
            "embedding_bgem3v2_768": cand_vec,
        },
        refresh=True,
    )

    await svc.rollback()

    # After rollback, queries read the stable field. ES 9 dense_vector
    # excludes vectors from `_source` by default; verify the dual-write
    # via kNN on each field instead — both must return the chunk.
    stable_hit = await es.search(
        index=index,
        body={
            "knn": {
                "field": "embedding",
                "query_vector": stable_vec,
                "k": 1,
                "num_candidates": 10,
            }
        },
    )
    cand_hit = await es.search(
        index=index,
        body={
            "knn": {
                "field": "embedding_bgem3v2_768",
                "query_vector": cand_vec,
                "k": 1,
                "num_candidates": 10,
            }
        },
    )
    assert stable_hit["hits"]["hits"][0]["_id"] == "chunk-during-cutover"
    assert cand_hit["hits"]["hits"][0]["_id"] == "chunk-during-cutover"


# ---------------------------------------------------------------------------
# Reconciler retired-field sweep
# ---------------------------------------------------------------------------


async def test_reconciler_sweep_clears_retired_field_values(lifecycle_setup) -> None:
    from ragent.reconciler import Reconciler

    svc = lifecycle_setup["service"]
    settings = lifecycle_setup["settings"]
    es = lifecycle_setup["es"]
    index = lifecycle_setup["index"]
    registry = lifecycle_setup["registry"]

    # Stage some chunks with the candidate field populated.
    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh()
    for i in range(3):
        await es.index(
            index=index,
            id=f"chunk-{i}",
            document={
                "document_id": f"DOC-{i}",
                "chunk_id": f"chunk-{i}",
                "text": f"chunk {i}",
                "embedding": [0.1] * 1024,
                "embedding_bgem3v2_768": [0.5] * 768,
            },
            refresh=True,
        )

    # Abort — candidate enters retired list, cleanup_done=false.
    await registry.refresh()
    await svc.abort()

    # Build a reconciler wired with the settings repo + es client.
    from unittest.mock import MagicMock

    rec = Reconciler(
        repo=MagicMock(),
        broker=MagicMock(),
        registry=None,
        settings_repo=settings,
        es_client=es,
        chunks_index=index,
    )
    await rec._sweep_retired_embedding_fields()

    # Wait briefly for _update_by_query refresh (the sweep does not refresh
    # by default — force it via a no-op refresh call).
    await es.indices.refresh(index=index)

    # Every chunk's candidate field is gone; stable still searchable.
    # ES dense_vector hides values from `_source`; verify via kNN
    # (no hits on the retired field; hits on the stable field).
    cand_hits = await es.search(
        index=index,
        body={
            "knn": {
                "field": "embedding_bgem3v2_768",
                "query_vector": [0.5] * 768,
                "k": 10,
                "num_candidates": 50,
            }
        },
    )
    assert cand_hits["hits"]["total"]["value"] == 0
    stable_hits = await es.search(
        index=index,
        body={
            "knn": {
                "field": "embedding",
                "query_vector": [0.1] * 1024,
                "k": 10,
                "num_candidates": 50,
            }
        },
    )
    assert stable_hits["hits"]["total"]["value"] == 3

    # Retired entry marked done.
    retired_after = await settings.get("embedding.retired")
    assert retired_after[0]["cleanup_done"] is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
