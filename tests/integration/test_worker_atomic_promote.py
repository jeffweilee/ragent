"""T-RR.9 (B39) — Atomic promote-and-demote on worker READY transition.

When a worker finishes a re-ingest of an existing (source_id, source_app),
the new doc's READY transition must — in the same DB transaction —
atomically demote any prior READY siblings to DELETING. Combined with B36
(_SourceHydrator drops chunks whose document_id is not READY), retrieval
transitions to the new revision the moment the worker's tx commits — no
race window where both old and new are READY and both retrievable.
"""

from __future__ import annotations

import pytest

from ragent.bootstrap.init_schema import _to_sync_dsn, init_mariadb
from ragent.repositories.document_repository import DocumentRepository

pytestmark = pytest.mark.docker


async def _seed(repo: DocumentRepository, doc_id: str, source_id: str, source_app: str) -> None:
    await repo.create(
        document_id=doc_id,
        create_user="alice",
        source_id=source_id,
        source_app=source_app,
        source_title="t",
        object_key=f"{source_app}_{source_id}_{doc_id}",
    )
    await repo.update_status(doc_id, from_status="UPLOADED", to_status="PENDING")


@pytest.fixture
def fresh_engine(mariadb_dsn: str):
    """Fresh schema + async engine for each test in this module."""
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import create_async_engine

    sync_dsn = _to_sync_dsn(mariadb_dsn)
    sync_engine = create_engine(sync_dsn)
    init_mariadb(sync_engine)
    # Tests in this module own the documents table — wipe between cases so
    # ID collisions don't leak across runs.
    with sync_engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("DELETE FROM documents"))
    sync_engine.dispose()

    engine = create_async_engine(mariadb_dsn)
    yield engine
    # Engine disposal during teardown straddles event loops on aiomysql; the
    # connection pool is short-lived per test and GC'd cleanly without an
    # explicit dispose, so we skip it here.


@pytest.mark.asyncio
async def test_promote_demotes_prior_ready_sibling(fresh_engine) -> None:
    """B39: A & B share (S1, confluence); A=READY, B finishes → A=DELETING, B=READY."""
    repo = DocumentRepository(engine=fresh_engine)

    await _seed(repo, "DOC_A", "S1", "confluence")
    await repo.update_status("DOC_A", from_status="PENDING", to_status="READY")

    await _seed(repo, "DOC_B", "S1", "confluence")

    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_B", source_id="S1", source_app="confluence"
    )

    a = await repo.get("DOC_A")
    b = await repo.get("DOC_B")
    assert a is not None and a.status == "DELETING"
    assert b is not None and b.status == "READY"


@pytest.mark.asyncio
async def test_promote_leaves_other_source_groups_untouched(fresh_engine) -> None:
    """Demote scope is exactly (source_id, source_app); siblings under other tuples remain READY."""
    repo = DocumentRepository(engine=fresh_engine)

    await _seed(repo, "DOC_A", "S1", "confluence")
    await repo.update_status("DOC_A", from_status="PENDING", to_status="READY")

    # Different source_app — must NOT be demoted.
    await _seed(repo, "DOC_SLACK", "S1", "slack")
    await repo.update_status("DOC_SLACK", from_status="PENDING", to_status="READY")

    await _seed(repo, "DOC_B", "S1", "confluence")

    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_B", source_id="S1", source_app="confluence"
    )

    slack = await repo.get("DOC_SLACK")
    assert slack is not None and slack.status == "READY"


@pytest.mark.asyncio
async def test_promote_idempotent_with_no_prior_ready(fresh_engine) -> None:
    """First-time ingest (no prior READY siblings) — promote still flips PENDING → READY."""
    repo = DocumentRepository(engine=fresh_engine)
    await _seed(repo, "DOC_FIRST", "S2", "confluence")

    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_FIRST", source_id="S2", source_app="confluence"
    )

    first = await repo.get("DOC_FIRST")
    assert first is not None and first.status == "READY"
