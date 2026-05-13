"""promote_to_ready_and_demote_siblings: deadlock retry (MariaDB error 1213).

When the UPDATE inside promote_to_ready_and_demote_siblings hits a deadlock,
the method must retry up to 3 times rather than propagating the exception,
leaving the document stuck in PENDING.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from ragent.repositories.document_repository import DocumentRepository


def _deadlock_exc() -> OperationalError:
    cause = MagicMock()
    cause.args = (1213, "Deadlock found when trying to get lock; try restarting transaction")
    return OperationalError("UPDATE documents ...", {}, cause)


def _make_engine(execute_side_effects: list) -> MagicMock:
    """Build a mock engine whose begin() returns a fresh conn each call.

    ``execute_side_effects`` is consumed left-to-right across ALL begin()
    contexts; each element is either an exception (to raise) or a return
    value for conn.execute.  ``rowcount=0`` → self-demoted; ``rowcount=1``
    → promoted + a second execute call for the sibling-demote UPDATE.
    """
    effects = list(execute_side_effects)

    async def execute_fn(*_a, **_kw) -> MagicMock:
        effect = effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def make_ctx() -> MagicMock:
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=execute_fn)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    engine = MagicMock()
    engine.begin = MagicMock(side_effect=lambda: make_ctx())
    return engine


def _rowcount(n: int) -> MagicMock:
    m = MagicMock()
    m.rowcount = n
    return m


@pytest.mark.asyncio
async def test_promote_retries_once_on_deadlock_then_succeeds():
    """Deadlock on first attempt → retry → promoted (True)."""
    engine = _make_engine(
        [
            _deadlock_exc(),  # attempt 0: raise deadlock
            _rowcount(1),  # attempt 1: promote UPDATE succeeds
            _rowcount(0),  # attempt 1: sibling-demote UPDATE (no siblings)
        ]
    )
    repo = DocumentRepository(engine=engine)

    result = await repo.promote_to_ready_and_demote_siblings("D1", "S1", "app")

    assert result is True
    assert engine.begin.call_count == 2


@pytest.mark.asyncio
async def test_self_demote_retries_once_on_deadlock_then_succeeds():
    """Deadlock on first attempt → retry → self-demoted (False)."""
    engine = _make_engine(
        [
            _deadlock_exc(),  # attempt 0: raise deadlock
            _rowcount(0),  # attempt 1: promote UPDATE → not winner
            _rowcount(1),  # attempt 1: self-demote UPDATE → demoted
        ]
    )
    repo = DocumentRepository(engine=engine)

    result = await repo.promote_to_ready_and_demote_siblings("D1", "S1", "app")

    assert result is False
    assert engine.begin.call_count == 2


@pytest.mark.asyncio
async def test_non_deadlock_operational_error_propagates_immediately():
    """Non-deadlock OperationalError (e.g. 1054 unknown column) propagates without retry."""
    cause = MagicMock()
    cause.args = (1054, "Unknown column")
    non_deadlock = OperationalError("SELECT", {}, cause)

    engine = _make_engine([non_deadlock])
    repo = DocumentRepository(engine=engine)

    with pytest.raises(OperationalError):
        await repo.promote_to_ready_and_demote_siblings("D1", "S1", "app")

    assert engine.begin.call_count == 1


@pytest.mark.asyncio
async def test_deadlock_exhausting_all_retries_reraises():
    """Three consecutive deadlocks → OperationalError propagates after max retries."""
    engine = _make_engine(
        [
            _deadlock_exc(),
            _deadlock_exc(),
            _deadlock_exc(),
        ]
    )
    repo = DocumentRepository(engine=engine)

    with pytest.raises(OperationalError) as exc_info:
        await repo.promote_to_ready_and_demote_siblings("D1", "S1", "app")

    assert engine.begin.call_count == 3
    assert exc_info.value.orig.args[0] == 1213
