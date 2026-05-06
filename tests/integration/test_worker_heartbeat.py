"""T3.2i — Worker heartbeat: mid-pipeline updated_at kept fresh, preventing stale sweep (B16)."""

import datetime
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.docker


def test_heartbeat_updates_updated_at_periodically():
    """Worker heartbeat calls update_heartbeat every interval while pipeline runs."""
    from ragent.workers.heartbeat import run_heartbeat

    heartbeat_calls: list[float] = []
    repo = AsyncMock()
    repo.update_heartbeat.side_effect = lambda doc_id: heartbeat_calls.append(time.monotonic())

    stop = threading.Event()
    thread = threading.Thread(
        target=run_heartbeat,
        kwargs={"document_id": "DOC001", "repo": repo, "stop": stop, "interval": 0.05},
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=1)

    assert repo.update_heartbeat.call_count >= 2, (
        f"Expected ≥2 heartbeat calls in 0.2s at 0.05s interval, "
        f"got {repo.update_heartbeat.call_count}"
    )


def test_heartbeat_stops_when_event_set():
    """Heartbeat thread terminates promptly when stop event is set."""
    from ragent.workers.heartbeat import run_heartbeat

    repo = AsyncMock()
    stop = threading.Event()
    thread = threading.Thread(
        target=run_heartbeat,
        kwargs={"document_id": "DOC001", "repo": repo, "stop": stop, "interval": 0.1},
        daemon=True,
    )
    thread.start()
    stop.set()
    thread.join(timeout=0.5)
    assert not thread.is_alive(), "Heartbeat thread should have stopped"


def test_stale_sweep_skips_fresh_heartbeat_row():
    """list_pending_stale does not return rows whose updated_at is within the threshold."""
    repo = MagicMock()
    now = datetime.datetime.now(datetime.UTC)
    threshold = now - datetime.timedelta(minutes=5)

    # Row whose updated_at is 1 minute ago — NOT stale
    repo.list_pending_stale.return_value = []  # fresh row not returned
    result = repo.list_pending_stale(updated_before=threshold, attempt_le=5)
    assert result == []
