"""T-EM.20 — End-to-end embedding-model lifecycle (B50 index-per-model).

Real MariaDB + Elasticsearch testcontainers exercising the lifecycle from
``promote`` through ``cutover``, ``rollback``, ``abort`` and the reconciler
``retired-index`` sweep. Asserts the invariants the design doc calls out:

- promote creates a new physical ES index (``chunks_v2``) and records
  ``embedding.candidate`` with ``index_name``
- cutover swaps the read alias ``chunks_v1_active`` from ``chunks_v1`` to
  ``chunks_v2``; rollback reverses it without touching the candidate index
- dual-write during CUTOVER: ingest writes to BOTH physical indices so
  rollback finds a current vector in the stable index via the alias
- commit retires the old stable index; reconciler sweep deletes the physical
  index and marks ``cleanup_done = true``

The real ingest pipeline is bypassed; we exercise the settings + ES seams
directly.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ragent.bootstrap.init_schema import patch_aiomysql_ping

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

_STABLE_INDEX = "chunks_v1"
_STABLE_SEED = {
    "name": "bge-m3",
    "dim": 1024,
    "api_url": "",
    "model_arg": "bge-m3",
    "index_name": _STABLE_INDEX,
}
_ALIAS = "chunks_v1_active"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lifecycle_dsn():
    """Dedicated MariaDB container so this module's alembic upgrade is
    isolated from the shared session-scoped DB."""
    from testcontainers.mysql import MySqlContainer

    from tc_utils import tc_image
    from tests.integration._alembic_utils import apply_alembic_head

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
        apply_alembic_head(sync_dsn)
        yield async_dsn


@pytest.fixture
async def lifecycle_engine(lifecycle_dsn):
    """Async SQLAlchemy engine — function-scoped so each test's connections
    bind to its own asyncio loop (`pytest-asyncio` default loop scope =
    function). Sharing an AsyncEngine across event loops raises
    ``RuntimeError('Event loop is closed')`` on the second test."""
    engine = create_async_engine(lifecycle_dsn, pool_pre_ping=True)
    patch_aiomysql_ping(engine)
    yield engine
    await engine.dispose()


def _stable_index_body(dim: int = 1024) -> dict:
    return {
        "mappings": {
            "properties": {
                "document_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "content": {"type": "text"},
                "title": {"type": "text"},
                "embedding": {
                    "type": "dense_vector",
                    "dims": dim,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "flat"},
                },
            }
        }
    }


@pytest.fixture
async def chunks_index(es_url):
    """Fresh ``chunks_v1`` index + ``chunks_v1_active`` alias per test.

    Promote() creates ``chunks_v2``; teardown removes both.
    """
    from elasticsearch import AsyncElasticsearch

    from ragent.bootstrap.init_schema import put_es_pipelines

    es = AsyncElasticsearch(hosts=[es_url])
    for idx in [_STABLE_INDEX, "chunks_v2"]:
        await es.indices.delete(index=idx, ignore_unavailable=True)

    # promote() creates chunks_v2 from resources/es/chunks_v1.json, whose
    # settings.index.default_pipeline references chunks_default (B59) — ES
    # rejects index creation if that pipeline doesn't exist yet.
    put_es_pipelines(es_url)

    await es.indices.create(index=_STABLE_INDEX, body=_stable_index_body())
    await es.indices.put_alias(index=_STABLE_INDEX, name=_ALIAS)

    yield es, _STABLE_INDEX, _ALIAS

    for idx in [_STABLE_INDEX, "chunks_v2"]:
        await es.indices.delete(index=idx, ignore_unavailable=True)
    await es.close()


@pytest.fixture
async def lifecycle_setup(lifecycle_engine, chunks_index):
    """Wire SystemSettingsRepository + ActiveModelRegistry + LifecycleService
    against the real MariaDB / ES containers."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.services.embedding.registry import ActiveModelRegistry

    es, index, alias = chunks_index

    settings = SystemSettingsRepository(engine=lifecycle_engine)
    await settings.transition(
        {
            "embedding.stable": _STABLE_SEED,
            "embedding.candidate": None,
            "embedding.read": "stable",
            "embedding.retired": [],
        }
    )

    registry = ActiveModelRegistry(
        settings_repo=settings,
        ttl_seconds=0,
        chunks_read_alias=alias,
        chunks_fallback_index=index,
    )
    await registry.refresh(force=True)

    service = EmbeddingLifecycleService(
        settings_repo=settings,
        es_client=es,
        index_name=index,
        registry=registry,
        cache_ttl_seconds=0,
    )
    return {
        "settings": settings,
        "registry": registry,
        "service": service,
        "es": es,
        "index": index,
        "alias": alias,
    }


def _alias_target(alias_response: dict) -> set[str]:
    """Return the set of index names the ``_ALIAS`` alias points to."""
    return {idx for idx, meta in alias_response.items() if _ALIAS in meta.get("aliases", {})}


# ---------------------------------------------------------------------------
# Promote → new physical index + settings
# ---------------------------------------------------------------------------


async def test_promote_creates_new_es_index_and_writes_candidate_setting(
    lifecycle_setup,
) -> None:
    svc = lifecycle_setup["service"]
    es = lifecycle_setup["es"]
    settings = lifecycle_setup["settings"]

    result = await svc.promote(
        name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2"
    )

    assert result["state"] == "CANDIDATE"

    # A new physical index was created (not a new field on chunks_v1).
    assert await es.indices.exists(index="chunks_v2")
    mapping = await es.indices.get_mapping(index="chunks_v2")
    props = mapping["chunks_v2"]["mappings"]["properties"]
    assert "embedding" in props
    assert props["embedding"]["dims"] == 768

    # The old stable index is untouched.
    assert await es.indices.exists(index=_STABLE_INDEX)

    stored = await settings.get("embedding.candidate")
    assert stored["name"] == "bge-m3-v2"
    assert stored["index_name"] == "chunks_v2"
    assert "field" not in stored


# ---------------------------------------------------------------------------
# Cutover → rollback → abort
# ---------------------------------------------------------------------------


async def test_full_lifecycle_promote_cutover_rollback_abort(lifecycle_setup) -> None:
    svc = lifecycle_setup["service"]
    settings = lifecycle_setup["settings"]
    registry = lifecycle_setup["registry"]
    es = lifecycle_setup["es"]
    alias = lifecycle_setup["alias"]

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh(force=True)

    cutover_result = await svc.cutover(force=True)
    assert cutover_result["state"] == "CUTOVER"
    assert await settings.get("embedding.read") == "candidate"

    alias_info = await es.indices.get_alias(name=alias)
    assert _alias_target(alias_info) == {"chunks_v2"}

    await registry.refresh(force=True)
    rollback_result = await svc.rollback()
    assert rollback_result["state"] == "CANDIDATE"
    assert await settings.get("embedding.read") == "stable"

    alias_info = await es.indices.get_alias(name=alias)
    assert _alias_target(alias_info) == {_STABLE_INDEX}

    assert (await settings.get("embedding.candidate")) is not None

    await registry.refresh(force=True)
    abort_result = await svc.abort()
    assert abort_result["state"] == "IDLE"
    assert await settings.get("embedding.candidate") is None

    retired = await settings.get("embedding.retired")
    assert len(retired) == 1
    assert retired[0]["name"] == "bge-m3-v2"
    assert retired[0]["index_name"] == "chunks_v2"
    assert retired[0]["cleanup_done"] is False

    # abort() deletes the candidate index immediately.
    assert not await es.indices.exists(index="chunks_v2")


# ---------------------------------------------------------------------------
# Rollback-window dual-write safety
# ---------------------------------------------------------------------------


async def test_doc_upsert_during_rollback_window_keeps_both_indices_current(
    lifecycle_setup,
) -> None:
    """Core B50 safety claim: a chunk written during CANDIDATE/CUTOVER state
    must exist in BOTH physical indices so rollback leaves the stable alias
    pointing at a valid, current vector."""
    svc = lifecycle_setup["service"]
    es = lifecycle_setup["es"]
    alias = lifecycle_setup["alias"]
    registry = lifecycle_setup["registry"]

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh(force=True)
    await svc.cutover(force=True)
    await registry.refresh(force=True)

    stable_vec = [0.1] * 1024
    cand_vec = [0.9] * 768
    doc = {
        "document_id": "DOC-XYZ",
        "chunk_id": "chunk-during-cutover",
        "content": "hello",
        "embedding": stable_vec,
    }
    # Simulate DocumentEmbedder dual-write: stable index + candidate index.
    await es.index(index=_STABLE_INDEX, id="chunk-during-cutover", document=doc, refresh=True)
    await es.index(
        index="chunks_v2",
        id="chunk-during-cutover",
        document={**doc, "embedding": cand_vec},
        refresh=True,
    )

    await svc.rollback()

    # After rollback, alias points back to chunks_v1.
    alias_info = await es.indices.get_alias(name=alias)
    assert _alias_target(alias_info) == {_STABLE_INDEX}

    # Query via the alias finds the chunk in the stable index.
    stable_hit = await es.search(
        index=alias,
        body={
            "knn": {
                "field": "embedding",
                "query_vector": stable_vec,
                "k": 1,
                "num_candidates": 10,
            }
        },
    )
    assert stable_hit["hits"]["hits"][0]["_id"] == "chunk-during-cutover"

    # Candidate index also retains the dual-written chunk.
    cand_hit = await es.search(
        index="chunks_v2",
        body={
            "knn": {
                "field": "embedding",
                "query_vector": cand_vec,
                "k": 1,
                "num_candidates": 10,
            }
        },
    )
    assert cand_hit["hits"]["hits"][0]["_id"] == "chunk-during-cutover"


# ---------------------------------------------------------------------------
# Reconciler retired-index sweep
# ---------------------------------------------------------------------------


async def test_reconciler_sweep_deletes_retired_stable_index(lifecycle_setup) -> None:
    """commit() retires the old stable index. The reconciler sweep must
    DELETE the physical index and mark cleanup_done = true."""
    from unittest.mock import MagicMock

    from ragent.reconciler import Reconciler

    svc = lifecycle_setup["service"]
    settings = lifecycle_setup["settings"]
    es = lifecycle_setup["es"]
    registry = lifecycle_setup["registry"]

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")
    await registry.refresh(force=True)
    await svc.cutover(force=True)
    await registry.refresh(force=True)
    await svc.commit()

    # After commit, old stable (chunks_v1) is in retired list; chunks_v2 is new stable.
    retired = await settings.get("embedding.retired")
    assert len(retired) == 1
    assert retired[0]["index_name"] == _STABLE_INDEX
    assert retired[0]["cleanup_done"] is False

    # The physical index still exists before the sweep.
    assert await es.indices.exists(index=_STABLE_INDEX)

    rec = Reconciler(
        repo=MagicMock(),
        broker=MagicMock(),
        registry=None,
        settings_repo=settings,
        es_client=es,
        chunks_index=_STABLE_INDEX,
    )
    await rec._sweep_retired_embedding_indices()

    # Physical index deleted.
    assert not await es.indices.exists(index=_STABLE_INDEX)

    # Retired entry marked done.
    retired_after = await settings.get("embedding.retired")
    assert retired_after[0]["cleanup_done"] is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
