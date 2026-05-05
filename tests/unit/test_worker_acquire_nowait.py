"""T3.2g — Worker: second worker fails fast on NOWAIT, re-kiqs with backoff (R7, S28)."""

from unittest.mock import MagicMock

from ragent.repositories.document_repository import LockNotAvailable


def _import_worker():
    from ragent.workers.ingest import handle_lock_contention

    return handle_lock_contention


def test_lock_contention_does_not_increment_attempt():
    """Second worker NOWAIT failure must not increment attempt (S28)."""
    handle_lock_contention = _import_worker()
    repo = MagicMock()
    repo.acquire_nowait.side_effect = LockNotAvailable("DOC1")

    delay = handle_lock_contention(document_id="DOC1", current_attempt=0, repo=repo)
    repo.update_status.assert_not_called()
    assert delay > 0  # exponential backoff scheduled


def test_lock_contention_backoff_increases_with_attempt():
    handle_lock_contention = _import_worker()
    repo = MagicMock()
    delay0 = handle_lock_contention(document_id="DOC1", current_attempt=0, repo=repo)
    delay1 = handle_lock_contention(document_id="DOC1", current_attempt=1, repo=repo)
    delay2 = handle_lock_contention(document_id="DOC1", current_attempt=2, repo=repo)
    assert delay0 < delay1 < delay2


def test_lock_contention_caps_at_30s():
    handle_lock_contention = _import_worker()
    repo = MagicMock()
    # At a very high attempt number the delay should be capped
    delay = handle_lock_contention(document_id="DOC1", current_attempt=100, repo=repo)
    assert delay <= 30
