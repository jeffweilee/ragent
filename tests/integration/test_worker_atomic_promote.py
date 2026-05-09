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


@pytest.mark.asyncio
async def test_older_worker_finishing_after_newer_pending_self_demotes(fresh_engine) -> None:
    """Out-of-order worker completion: older worker must NOT promote when a newer
    revision is in flight; it self-demotes so the newer worker's tx is the one
    that flips retrieval. Reconciler is safety-net only — correctness holds
    from the worker's tx alone.
    """
    import asyncio

    repo = DocumentRepository(engine=fresh_engine)

    await _seed(repo, "DOC_OLD", "S3", "confluence")
    await asyncio.sleep(0.01)
    await _seed(repo, "DOC_NEW", "S3", "confluence")

    # Older worker finishes first (out of order).
    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_OLD", source_id="S3", source_app="confluence"
    )

    old = await repo.get("DOC_OLD")
    new = await repo.get("DOC_NEW")
    assert old is not None and old.status == "DELETING"
    assert new is not None and new.status == "PENDING"


@pytest.mark.asyncio
async def test_older_worker_finishing_after_newer_ready_self_demotes(fresh_engine) -> None:
    """Out-of-order: newer worker already promoted (READY); older worker must
    self-demote and leave the newer READY untouched.
    """
    import asyncio

    repo = DocumentRepository(engine=fresh_engine)

    await _seed(repo, "DOC_OLD", "S4", "confluence")
    await asyncio.sleep(0.01)
    await _seed(repo, "DOC_NEW", "S4", "confluence")

    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_NEW", source_id="S4", source_app="confluence"
    )
    # Older worker finishes after newer is already READY.
    await repo.promote_to_ready_and_demote_siblings(
        document_id="DOC_OLD", source_id="S4", source_app="confluence"
    )

    old = await repo.get("DOC_OLD")
    new = await repo.get("DOC_NEW")
    assert old is not None and old.status == "DELETING"
    assert new is not None and new.status == "READY"


@pytest.mark.asyncio
async def test_for_update_serializes_concurrent_promotes(fresh_engine) -> None:
    """Lock semantic: a second concurrent promote on the same (source_id,
    source_app) must block on the FOR UPDATE row lock until the first tx
    commits. Validates that retrieval correctness is enforced by the DB,
    not by application-level serialization.
    """
    import asyncio

    from sqlalchemy import text

    repo = DocumentRepository(engine=fresh_engine)
    await _seed(repo, "DOC_A", "S5", "confluence")
    await asyncio.sleep(0.01)
    await _seed(repo, "DOC_B", "S5", "confluence")  # newer

    t1_locked = asyncio.Event()
    t1_release = asyncio.Event()

    async def hold_for_update() -> None:
        async with fresh_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    SELECT document_id FROM documents
                    WHERE source_id=:src AND source_app=:app
                      AND status IN ('PENDING','READY')
                    ORDER BY created_at DESC, document_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """
                ),
                {"src": "S5", "app": "confluence"},
            )
            t1_locked.set()
            await t1_release.wait()
            # tx commits on context exit, releasing the lock

    holder = asyncio.create_task(hold_for_update())
    await t1_locked.wait()

    promote_task = asyncio.create_task(
        repo.promote_to_ready_and_demote_siblings(
            document_id="DOC_B", source_id="S5", source_app="confluence"
        )
    )
    # Give the second tx a chance to run; it must NOT complete while T1 holds
    # the row lock.
    await asyncio.sleep(0.3)
    assert not promote_task.done(), "FOR UPDATE failed to block second promote"

    t1_release.set()
    await holder
    promoted = await asyncio.wait_for(promote_task, timeout=5.0)

    assert promoted is True
    a = await repo.get("DOC_A")
    b = await repo.get("DOC_B")
    # A remains PENDING (it has its own worker pending; A's promote will then
    # see B already READY and self-demote). B is the survivor and is READY.
    assert a is not None and a.status == "PENDING"
    assert b is not None and b.status == "READY"
